import asyncio
import os
import re
from urllib.parse import urlparse, urljoin, quote
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="NicoStream")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

NICO_SEARCH_API = "https://snapshot.search.nicovideo.jp/api/v2/snapshot/video/contents/search"

async def nico_search(query: str, limit: int):
    # The old approach fed https://www.nicovideo.jp/search/<query> straight into
    # yt-dlp's generic/flat extractor. That page renders its results with
    # client-side JS, so a flat HTML scrape finds no video entries and silently
    # returns an empty list (200 OK, 0 results) instead of raising.
    # Use Niconico's official public Snapshot Search JSON API instead, which
    # returns results directly with no JS rendering involved.
    params = {
        "q": query,
        "targets": "title,description,tags",
        "fields": "contentId,title,userId,lengthSeconds,thumbnailUrl,viewCounter",
        "_sort": "-viewCounter",
        "_offset": "0",
        "_limit": str(limit),
        "_context": "nicostream",
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NicoStream/1.0)"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(NICO_SEARCH_API, params=params, headers=headers)
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
            "id": content_id, "title": item.get("title") or "Untitled",
            "url": f"https://www.nicovideo.jp/watch/{content_id}",
            "thumbnail": item.get("thumbnailUrl"),
            "duration": item.get("lengthSeconds"),
            "uploader": None
        })
    return {"results": results}

def get_info(video_url: str):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(video_url, download=False)

@app.get("/api/info")
async def info(url: str = Query(...)):
    if not url.startswith(("https://www.nicovideo.jp/", "http://www.nicovideo.jp/")):
        raise HTTPException(status_code=400, detail="Only NicoNico URLs are accepted")
    try:
        data = await asyncio.to_thread(get_info, url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not read video: {e}")
    formats = []
    for f in data.get("formats", []):
        if not f.get("url"): continue
        formats.append({
            "format_id": f.get("format_id"), "ext": f.get("ext"),
            "protocol": f.get("protocol"), "height": f.get("height"),
            "width": f.get("width"), "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"), "tbr": f.get("tbr"), "url": f.get("url")
        })
    return {
        "id": data.get("id"), "title": data.get("title"),
        "thumbnail": data.get("thumbnail"), "duration": data.get("duration"),
        "formats": formats
    }

# Niconico's delivery CDN (domand) rejects requests with no Referer/User-Agent,
# and its playlists use byte-range-addressed fMP4 segments, so Range support
# is required too.
UPSTREAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://www.nicovideo.jp/",
    "Origin": "https://www.nicovideo.jp",
}

_M3U8_URI_ATTR = re.compile(r'URI="([^"]+)"')

def _proxy_url(absolute_url: str) -> str:
    return "/api/stream?url=" + quote(absolute_url, safe="")

def _rewrite_manifest(text: str, base_url: str) -> str:
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            m = _M3U8_URI_ATTR.search(line)
            if m:
                abs_uri = urljoin(base_url, m.group(1))
                line = line[:m.start(1)] + _proxy_url(abs_uri) + line[m.end(1):]
            out_lines.append(line)
        elif stripped:
            abs_uri = urljoin(base_url, stripped)
            out_lines.append(_proxy_url(abs_uri))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)

def _is_manifest(content_type: str, url: str, body_start: bytes) -> bool:
    if "mpegurl" in content_type.lower():
        return True
    if url.split("?", 1)[0].endswith(".m3u8"):
        return True
    return body_start.lstrip().startswith(b"#EXTM3U")

@app.get("/api/stream")
async def stream(request: Request, url: str = Query(...)):
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Invalid media URL")

    headers = dict(UPSTREAM_HEADERS)
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=30)
    try:
        req = client.build_request("GET", url, headers=headers)
        upstream = await client.send(req, stream=True)
        if upstream.status_code >= 400:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail=f"Upstream returned {upstream.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Stream failed: {e}")

    content_type = upstream.headers.get("content-type", "application/octet-stream")

    # Manifests are small text files: buffer, rewrite embedded URLs so segment/key
    # requests come back through this proxy (with the same required headers),
    # then close the upstream connection.
    if _is_manifest(content_type, str(upstream.url), b""):
        body = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        if _is_manifest(content_type, str(upstream.url), body):
            rewritten = _rewrite_manifest(body.decode("utf-8", "ignore"), str(upstream.url))
            return HTMLResponse(content=rewritten, media_type="application/vnd.apple.mpegurl")
        return StreamingResponse(iter([body]), media_type=content_type)

    # Media segments: stream through, forwarding range/length headers so seeking
    # and byte-range fMP4 segments keep working.
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

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
