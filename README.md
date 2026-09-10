# NicoStream — Railway / Japan Proxy build

Railway-ready NicoStream build. Railway detects the root `Dockerfile` automatically.

## Behavior

- Gets fresh public HTTP proxies with country filter `JP` from ProxyScrape.
- Verifies a candidate's exit country is Japan and checks Niconico connectivity before using it.
- Uses the selected Japanese proxy for Niconico search, yt-dlp metadata extraction, and playback.
- `/api/info` creates a playback session and pins that session to one proxy.
- HLS manifests are rewritten so child playlist/segment requests stay on the same proxy/session.
- A new playback session can select a new proxy after the current proxy expires/fails.
- Proxy candidates are refreshed after `PROXY_REFRESH_SECONDS` (default 300 seconds).

## Railway

Push the contents of this ZIP to the root of a GitHub repository and connect that repository to Railway.
No custom Start Command is required: Railway automatically detects a root `Dockerfile` and uses its `CMD`.

Optional Railway variables:

- `PROXY_REFRESH_SECONDS=300`
- `PROXY_TEST_TIMEOUT=6`
- `PROXY_CANDIDATES=40`
- `SESSION_TTL=21600`
- `PROXY_API_URL=` to replace the default ProxyScrape endpoint

Public proxies are inherently unreliable and may be slow, blocked, or disappear. The app fails cleanly when no working Japanese proxy is available rather than silently using a non-Japanese proxy.

Railway's Dockerfile detection and default start behavior are documented here:
https://docs.railway.com/builds/dockerfiles
