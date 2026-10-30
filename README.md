# X1 browser host — your own Chromium runtime

One container, no subscription, no per-minute vendor. It holds the browsers, saves the
sign-ins, serves the live frames, and reports its own usage. The app talks to it with a token
you generate.

## What it exposes

| Route | Purpose |
| --- | --- |
| `GET /health` | Liveness and how many sessions are open |
| `POST /sessions` | Open a browser (`timeout_seconds`, `keep_alive`, `context_id`, `viewport`, `allowed_domains`) |
| `GET /sessions` | List sessions (`status`, `limit`) |
| `GET /sessions/:id` | One session with its recent actions |
| `POST /sessions/:id/actions` | Drive the page: `goto`, `click`, `click_text`, `fill`, `type`, `press`, `select`, `scroll`, `wait_for`, `wait`, `extract_text`, `extract_links`, `snapshot`, `screenshot`, `back`, `forward`, `reload`, `url` |
| `GET /sessions/:id/frame.jpg` | Live JPEG — accepts the bearer token or a short-lived `?exp=&sig=` pair the app signs |
| `POST /sessions/:id/release` | Close the browser and persist its context |
| `GET /sessions/:id/logs` · `/downloads` | Action log, produced files |
| `POST /contexts` · `GET /contexts/:id` · `DELETE /contexts/:id` | Saved cookies and sign-ins (`storageState` on the volume) |
| `GET /project` · `POST /project` | Name, default timeout, allowed domains |
| `GET /usage` | Browser minutes, sessions opened, actions run |

## Environment

| Variable | Required | Meaning |
| --- | --- | --- |
| `HOST_TOKEN` | yes | Long random string. The app sends it as `Authorization: Bearer`. Generate with `openssl rand -hex 32`. |
| `PORT` | no | Default `8080`. |
| `DATA_DIR` | no | Default `/data`. Mount a volume here so contexts survive restarts. |
| `MAX_SESSIONS` | no | Default `5`. Concurrency cap. |
| `HARD_CAP_SECONDS` | no | Default `1800`. No session can outlive this. |
| `DEFAULT_TIMEOUT_SECONDS` | no | Default `600`. |

## Deploy

Any Docker host works. Railway is the shortest path:

1. New project → **Deploy from repo** → this repository.
2. Set **Root Directory** to `browser-host` (it picks up the `Dockerfile`).
3. Add `HOST_TOKEN` (the value from `openssl rand -hex 32`).
4. Add a **volume** mounted at `/data`.
5. Deploy, then copy the public URL.

On a VPS instead:

```bash
docker build -t x1-browser-host ./browser-host
docker run -d --name x1-browser-host -p 8080:8080 \
  -e HOST_TOKEN=<your token> -v x1-host-data:/data x1-browser-host
```

Fly.io and Render follow the same shape: Dockerfile, one env var, one volume.

## Connect it to the app

In the app's secrets, set:

- `BROWSER_HOST_URL` — the public https URL of the container (no trailing slash)
- `BROWSER_HOST_TOKEN` — the same value as `HOST_TOKEN`

Then open `/admin` → ask "what is my browser usage" → it should answer with the host's own numbers.

## Notes

- The browser launches once per container and each session gets its own context, so cookies
  never leak between sessions.
- A session that stops being used for 30 minutes is closed automatically unless it is keep-alive.
- Sessions are hard-capped by `HARD_CAP_SECONDS` even if a client asks for longer.
- `allowed_domains` on a session (or on the project) restricts where a run may navigate.