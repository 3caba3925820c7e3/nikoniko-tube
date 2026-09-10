import os
import asyncio
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import yt_dlp

app = FastAPI(title="NicoStream")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(12, ge=1, le=30)):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
    }
    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"nicoquery{limit}:{q}", download=False)
    try:
        data = await asyncio.to_thread(run)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Search failed: {e}")

    results = []
    for item in (data.get("entries") or []):
        if not item:
            continue
        results.append({
            "id": item.get("id"),
            "title": item.get("title") or "Untitled",
            "url": item.get("webpage_url") or item.get("url"),
            "thumbnail": item.get("thumbnail"),
            "duration": item.get("duration"),
            "uploader": item.get("uploader") or item.get("channel"),
        })
    return {"results": results}

def get_info(video_url: str):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(video_url, download=False)

@app.get("/api/info")
async def info(url: str = Query(...)):
    try:
        data = await asyncio.to_thread(get_info, url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not read video: {e}")

    formats = []
    for f in data.get("formats", []):
        if not f.get("url"):
            continue
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "protocol": f.get("protocol"),
            "height": f.get("height"),
            "width": f.get("width"),
            "fps": f.get("fps"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "abr": f.get("abr"),
            "tbr": f.get("tbr"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "url": f.get("url"),
        })
    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "formats": formats,
    }

@app.get("/api/stream")
async def stream(url: str = Query(...)):
    # Proxy the media through the server so the browser does not need to
    # access a provider's signed URL directly.
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Invalid media URL")

    try:
        data = await asyncio.to_thread(get_info, url)
        media_url = data.get("url")
        if not media_url:
            # Prefer a combined format when yt-dlp provides one.
            formats = data.get("formats") or []
            combined = [f for f in formats if f.get("url") and f.get("vcodec") != "none" and f.get("acodec") != "none"]
            if combined:
                media_url = combined[-1]["url"]
        if not media_url:
            raise ValueError("No playable media URL found")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stream preparation failed: {e}")

    import httpx

    async def iterator():
        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
            async with client.stream("GET", media_url) as r:
                if r.status_code >= 400:
                    raise HTTPException(status_code=502, detail=f"Upstream returned {r.status_code}")
                async for chunk in r.aiter_bytes(1024 * 256):
                    yield chunk

    headers = {}
    if data.get("ext"):
        headers["Content-Type"] = {
            "mp4": "video/mp4",
            "webm": "video/webm",
            "m3u8": "application/vnd.apple.mpegurl",
        }.get(data["ext"], "application/octet-stream")

    return StreamingResponse(iterator(), headers=headers)

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
