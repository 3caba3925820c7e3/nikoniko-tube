import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse, urljoin, quote

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nicostream")

app = FastAPI(title="NicoStream")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

NICO_SEARCH_API = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search"
PROXY_REFRESH_SECONDS = int(os.getenv("PROXY_REFRESH_SECONDS", "300"))
PROXY_TEST_TIMEOUT = float(os.getenv("PROXY_TEST_TIMEOUT", "8"))
PROXY_CANDIDATES = int(os.getenv("PROXY_CANDIDATES", "120"))
PROXY_TEST_CONCURRENCY = int(os.getenv("PROXY_TEST_CONCURRENCY", "30"))

# Multiple public sources are used because a country/protocol shard can temporarily
# be empty. The metadata endpoints are only used to discover candidates; every
# candidate is still tested from this Railway instance before it is accepted.
PROXY_SOURCES = [
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&country=jp&protocol=http",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&country=jp&protocol=https",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&country=jp&protocol=socks5",
    "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&country=JP&protocols=http",
    "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&country=JP&protocols=https",
    "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&country=JP&protocols=socks5",
]

UPSTREAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://www.nicovideo.jp/",
    "Origin": "https://www.nicovideo.jp",
}

@dataclass
class ProxyState:
    url: str
    checked_at: float

_proxy_state: ProxyState | None = None
_proxy_lock = asyncio.Lock()
_sessions: dict[str, dict] = {}
_sessions_lock = asyncio.Lock()
SESSION_TTL = int(os.getenv("SESSION_TTL", "21600"))

def _normalize_proxy(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" not in line:
        line = "http://" + line
    p = urlparse(line)
    if p.scheme not in ("http", "https") or not p.hostname or not p.port:
        return None
    return f"{p.scheme}://{p.hostname}:{p.port}"

async def _fetch_proxy_candidates() -> list[str]:
    headers = {"User-Agent": UPSTREAM_HEADERS["User-Agent"], "Accept": "application/json,text/plain,*/*"}
    candidates = []
    seen = set()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for source in PROXY_SOURCES:
            try:
                r = await client.get(source, headers=headers)
                r.raise_for_status()
                if "geonode.com" in source:
                    payload = r.json()
                    rows = payload.get("data", []) if isinstance(payload, dict) else []
                    for row in rows:
                        ip, port = row.get("ip"), row.get("port")
                        protocols = [str(x).lower() for x in (row.get("protocols") or [])]
                        for proto in protocols:
                            if proto in ("http", "https", "socks5") and ip and port:
                                p = f"{proto}://{ip}:{port}"
                                if p not in seen:
                                    seen.add(p); candidates.append(p)
                else:
                    for line in r.text.splitlines():
                        p = _normalize_proxy(line)
                        if p and p not in seen:
                            seen.add(p); candidates.append(p)
                logger.info("Proxy source returned candidates: %s (%d total)", source.split('/')[2], len(candidates))
            except Exception as e:
                logger.warning("Proxy source failed: %s | %s", source, e)
    return candidates[:PROXY_CANDIDATES]

async def _test_proxy(proxy: str) -> bool:
    # Validate both the exit country and the actual target. Some public proxies
    # answer generic IP checks but cannot CONNECT to the target site.
    try:
        timeout = httpx.Timeout(PROXY_TEST_TIMEOUT, connect=PROXY_TEST_TIMEOUT)
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True) as c:
            country = None
            for check_url in ("https://ipapi.co/country/", "https://ipinfo.io/country"):
                try:
                    r = await c.get(check_url, headers=UPSTREAM_HEADERS)
                    if r.status_code == 200:
                        country = r.text.strip().upper()
                        break
                except Exception:
                    pass
            if country != "JP":
                return False
            nico = await c.get("https://snapshot.search.nicovideo.jp/", headers=UPSTREAM_HEADERS)
            return nico.status_code < 500
    except Exception:
        return False

async def _choose_jp_proxy(force: bool = False) -> str:
    global _proxy_state
    now = time.time()
    if not force and _proxy_state and now - _proxy_state.checked_at < PROXY_REFRESH_SECONDS:
        return _proxy_state.url

    async with _proxy_lock:
        now = time.time()
        if not force and _proxy_state and now - _proxy_state.checked_at < PROXY_REFRESH_SECONDS:
            return _proxy_state.url

        candidates = await _fetch_proxy_candidates()
        # Test many candidates concurrently; public proxies are volatile and a
        # serial check can take several minutes before finding one that works.
        for start in range(0, len(candidates), PROXY_TEST_CONCURRENCY):
            batch = candidates[start:start + PROXY_TEST_CONCURRENCY]
            results = await asyncio.gather(*(_test_proxy(p) for p in batch), return_exceptions=True)
            for proxy, ok in zip(batch, results):
                if ok is True:
                    _proxy_state = ProxyState(proxy, time.time())
                    logger.info("Selected JP proxy: %s", proxy)
                    return proxy

        raise RuntimeError("No working Japanese HTTP proxy is currently available")

async def _session_proxy(session_id: str | None) -> str:
    if not session_id:
        return await _choose_jp_proxy()
    async with _sessions_lock:
        s = _sessions.get(session_id)
        if not s:
            raise HTTPException(status_code=410, detail="Playback session expired")
        s["last_seen"] = time.time()
        return s["proxy"]

async def _new_session(proxy: str, allowed_urls: list[str]) -> str:
    sid = uuid.uuid4().hex
    async with _sessions_lock:
        _sessions[sid] = {
            "proxy": proxy,
            "allowed": set(allowed_urls),
            "created": time.time(),
            "last_seen": time.time(),
        }
    return sid

async def _cleanup_sessions():
    while True:
        await asyncio.sleep(600)
        cutoff = time.time() - SESSION_TTL
        async with _sessions_lock:
            dead = [k for k, v in _sessions.items() if v["last_seen"] < cutoff]
            for k in dead:
                _sessions.pop(k, None)

@app.on_event("startup")
async def startup():
    asyncio.create_task(_cleanup_sessions())
    # Warm the proxy asynchronously; the first request still works if it has to wait.
    asyncio.create_task(_warm_proxy())

async def _warm_proxy():
    try:
        await _choose_jp_proxy()
    except Exception as e:
        logger.warning("Initial JP proxy warm-up failed: %s", e)

async def nico_search(query: str, limit: int):
    proxy = await _choose_jp_proxy()
    params = {
        "q": query,
        "targets": "title,description,tags",
        "fields": "contentId,title,userId,lengthSeconds,thumbnailUrl,viewCounter",
        "_sort": "-viewCounter",
        "_offset": "0",
        "_limit": str(limit),
        "_context": "nicostream",
    }
    async with httpx.AsyncClient(proxy=proxy, timeout=15) as client:
        resp = await client.get(NICO_SEARCH_API, params=params, headers=UPSTREAM_HEADERS)
        resp.raise_for_status()
        return resp.json()

@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(12, ge=1, le=30)):
    try:
        data = await nico_search(q, limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Search failed: {e}")
    results = []
    for item in data.get("data") or []:
        content_id = item.get("contentId")
        if not content_id:
            continue
        results.append({
            "id": content_id,
            "title": item.get("title") or "Untitled",
            "url": f"https://www.nicovideo.jp/watch/{content_id}",
            "thumbnail": item.get("thumbnailUrl"),
            "duration": item.get("lengthSeconds"),
            "uploader": None
        })
    return {"results": results}

def get_info(video_url: str, proxy: str):
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "proxy": proxy,
        "http_headers": UPSTREAM_HEADERS,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(video_url, download=False)

@app.get("/api/info")
async def info(url: str = Query(...)):
    if not url.startswith(("https://www.nicovideo.jp/", "http://www.nicovideo.jp/")):
        raise HTTPException(status_code=400, detail="Only NicoNico URLs are accepted")
    try:
        proxy = await _choose_jp_proxy()
        data = await asyncio.to_thread(get_info, url, proxy)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not read video: {e}")

    formats = []
    allowed_urls = []
    for f in data.get("formats", []):
        media_url = f.get("url")
        if not media_url:
            continue
        formats.append({
            "format_id": f.get("format_id"), "ext": f.get("ext"),
            "protocol": f.get("protocol"), "height": f.get("height"),
            "width": f.get("width"), "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"), "tbr": f.get("tbr"), "url": media_url
        })
        allowed_urls.append(media_url)

    session_id = await _new_session(proxy, allowed_urls)
    return {
        "id": data.get("id"), "title": data.get("title"),
        "thumbnail": data.get("thumbnail"), "duration": data.get("duration"),
        "session_id": session_id, "formats": formats
    }

_M3U8_URI_ATTR = re.compile(r'URI="([^"]+)"')

def _proxy_url(absolute_url: str, session_id: str) -> str:
    return "/api/stream?sid=" + quote(session_id, safe="") + "&url=" + quote(absolute_url, safe="")

def _rewrite_manifest(text: str, base_url: str, session_id: str) -> str:
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            m = _M3U8_URI_ATTR.search(line)
            if m:
                abs_uri = urljoin(base_url, m.group(1))
                line = line[:m.start(1)] + _proxy_url(abs_uri, session_id) + line[m.end(1):]
            out_lines.append(line)
        elif stripped:
            abs_uri = urljoin(base_url, stripped)
            out_lines.append(_proxy_url(abs_uri, session_id))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)

def _is_manifest(content_type: str, url: str, body_start: bytes) -> bool:
    return ("mpegurl" in content_type.lower()
            or url.split("?", 1)[0].endswith(".m3u8")
            or body_start.lstrip().startswith(b"#EXTM3U"))

@app.get("/api/stream")
async def stream(request: Request, sid: str = Query(...), url: str = Query(...)):
    proxy = await _session_proxy(sid)
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Invalid media URL")

    # Only URLs returned by yt-dlp for this exact playback session are accepted.
    async with _sessions_lock:
        session = _sessions.get(sid)
        allowed = session["allowed"] if session else set()
    # HLS child URLs are generated by the upstream manifest, so allow them after
    # the first authorized media URL has established the session's host.
    if url not in allowed:
        if not any(urlparse(a).hostname == urlparse(url).hostname for a in allowed):
            raise HTTPException(status_code=403, detail="URL is not part of this playback session")

    headers = dict(UPSTREAM_HEADERS)
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(proxy=proxy, follow_redirects=True, timeout=30)
    try:
        upstream = await client.send(client.build_request("GET", url, headers=headers), stream=True)
        if upstream.status_code >= 400:
            body_snippet = (await upstream.aread())[:500]
            await upstream.aclose()
            logger.warning("Upstream %s for %s", upstream.status_code, url)
            raise HTTPException(status_code=502, detail=f"Upstream returned {upstream.status_code}: {body_snippet[:200]!r}")
    except HTTPException:
        await client.aclose()
        raise
    except Exception as e:
        await client.aclose()
        logger.exception("Stream request failed")
        raise HTTPException(status_code=502, detail=f"Stream failed: {type(e).__name__}: {e}")

    content_type = upstream.headers.get("content-type", "application/octet-stream")
    if _is_manifest(content_type, str(upstream.url), b""):
        body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        if _is_manifest(content_type, str(upstream.url), body):
            rewritten = _rewrite_manifest(body.decode("utf-8", "ignore"), str(upstream.url), sid)
            return Response(content=rewritten, media_type="application/vnd.apple.mpegurl")
        return StreamingResponse(iter([body]), media_type=content_type)

    async def body_iter():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    passthrough_headers = {}
    for h in ("content-length", "content-range", "accept-ranges"):
        if h in upstream.headers:
            passthrough_headers[h] = upstream.headers[h]
    return StreamingResponse(body_iter(), status_code=upstream.status_code,
                             media_type=content_type, headers=passthrough_headers)

@app.get("/healthz")
async def healthz():
    return {"ok": True, "proxy_ready": _proxy_state is not None}

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
