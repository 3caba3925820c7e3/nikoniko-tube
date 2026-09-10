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
