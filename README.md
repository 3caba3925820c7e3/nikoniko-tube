# NicoStream — Railway / Japan Proxy build

Railway-ready NicoStream build. The root `Dockerfile` is used automatically by Railway.

## Behavior

- Pulls Japanese proxy candidates from multiple public sources (ProxyScrape + GeoNode).
- Tries HTTP, HTTPS and SOCKS5 candidates; SOCKS5 support is included in dependencies.
- Verifies the exit country is Japan and then checks Niconico connectivity before accepting a proxy.
- Uses the selected Japanese proxy for search and yt-dlp metadata extraction.
- `/api/info` creates a playback session pinned to one proxy.
- HLS manifests are rewritten so child playlist/segment requests keep the same session proxy.
- The app never silently falls back to a non-Japanese proxy.
- Candidate discovery refreshes every 5 minutes by default.

## Important

Free public proxies can disappear, reject HTTPS CONNECT, be rate-limited, or be too slow for video. If all current Japanese candidates are dead, the app correctly returns `No working Japanese HTTP proxy is currently available` instead of using a non-Japanese IP.

## Railway

Push the contents of this ZIP to the root of a GitHub repository and connect that repository to Railway. The root `Dockerfile` starts Uvicorn, so a custom Railway Start Command is not required.

Optional variables:

- `PROXY_REFRESH_SECONDS=300`
- `PROXY_TEST_TIMEOUT=8`
- `PROXY_CANDIDATES=120`
- `PROXY_TEST_CONCURRENCY=30`
- `SESSION_TTL=21600`

The public proxy sources are third-party services and are not guaranteed to have a live Japanese endpoint at every moment.
