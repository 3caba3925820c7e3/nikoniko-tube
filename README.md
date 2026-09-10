# NicoStream

A small FastAPI + yt-dlp web app for searching NicoNico and preparing playable media streams.

## Railway

Push this repository to GitHub and connect it to Railway. Railway will detect Python/Nixpacks and use the included start command.

Environment variables are not required.

## Local

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

## Notes

yt-dlp changes as providers change. If NicoNico changes its endpoints or authentication requirements, update yt-dlp to a newer compatible release.
