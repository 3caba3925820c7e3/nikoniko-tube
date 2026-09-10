import asyncio
import os
from urllib.parse import urlparse
import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="NicoStream")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def nico_search(query: str, limit: int):
    # Search NicoNico's public search page; do not use the unsupported nicoquery scheme.
    url = "https://www.nicovideo.jp/search/" + query
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": True, "playlistend": limit
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), limit: int = Query(12, ge=1, le=30)):
    try:
        data = await asyncio.to_thread(nico_search, q, limit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Search failed: {e}")
    results = []
    for item in data.get("entries") or []:
        if not item: continue
        vid = item.get("id")
        url = item.get("webpage_url") or item.get("url")
        if not url and vid:
            url = f"https://www.nicovideo.jp/watch/{vid}"
        results.append({
            "id": vid, "title": item.get("title") or "Untitled",
            "url": url, "thumbnail": item.get("thumbnail"),
            "duration": item.get("duration"),
            "uploader": item.get("uploader") or item.get("channel")
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

@app.get("/api/stream")
async def stream(url: str = Query(...)):
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Invalid media URL")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
            upstream = await client.get(url)
            upstream.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Stream failed: {e}")
    return StreamingResponse(iter([upstream.content]),
                             media_type=upstream.headers.get("content-type", "application/octet-stream"))

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8") as f:
        return f.read()

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
