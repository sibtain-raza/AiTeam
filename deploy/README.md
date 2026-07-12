# Deploying the Looper web control plane

Containerized deployment of the web UI (`server/` + `web/`). The CLI
(`python -m looper.main`) needs none of this — it's for hosting the
multi-user web app.

## Quick start

```sh
# 1. Authenticate the claude CLI on the HOST (the container mounts this):
claude auth status || claude auth login

# 2. From the repo root:
LOOPER_JWT_SECRET=$(openssl rand -hex 32) docker compose -f deploy/docker-compose.yml up --build
```

Web UI: <http://localhost:5173> · API: <http://localhost:8000> · health: `GET /health`.

## What's in here

| File | Purpose |
|---|---|
| `Dockerfile.server` | FastAPI app + pipeline + `claude` CLI, non-root, HEALTHCHECK |
| `Dockerfile.web` | Two-stage: Vite build → nginx static serving |
| `nginx.conf` | SPA fallback routing + asset caching |
| `docker-compose.yml` | Both services, volumes, env, restart policy |

## Configuration (all via env at `docker compose` time)

- `LOOPER_JWT_SECRET` — **required**, compose refuses to start without it.
- `LOOPER_CODE_MODEL` — model for all agents (default `sonnet`).
- `LOOPER_MAX_PARALLEL_SESSIONS` — cap concurrent Claude Code sessions (default unbounded).
- `LOOPER_CORS_ORIGINS` — comma-separated allowed origins (default `http://localhost:5173`).
- `LOOPER_PUBLIC_API_BASE` — the API URL **as the user's browser sees it**
  (build-time, inlined into the JS bundle; default `http://localhost:8000`).
  Change it when deploying behind a real hostname, then rebuild the web image.

QA's optional Playwright-based visual verification (see CLAUDE.md's "Visual
QA" note) caches its Chromium download at `/app/output/.playwright-browsers/`
— already inside the `looper-output` volume mounted above, so it survives
container restarts automatically. No extra volume or config needed here.

## Operational notes — read before hosting this anywhere real

- **Claude auth is a bind mount, not a secret you can inject.** The
  pipeline drives the `claude` CLI through its OAuth login; compose mounts
  the host's `~/.claude` read-write (the CLI refreshes tokens). Runs bill
  against that account.
- **Single API replica by design (Phase 1).** The SSE event broker and
  background run tasks are in-process, and the default DB is file-backed
  SQLite on a volume. Scaling horizontally requires externalizing the
  broker (e.g. Redis pub/sub) and setting `LOOPER_DB_URL` to a real
  database first — replicating the container as-is will split-brain live
  event streams.
- **Pipeline workspaces run untrusted-generated code.** Engineers/QA get
  real Bash inside the container (see CLAUDE.md's conventions section);
  the container boundary is your isolation layer here, which is a real
  improvement over running on the host — but don't co-locate this
  container with sensitive workloads.
- **TLS is out of scope here.** Put a reverse proxy (Caddy, Traefik,
  nginx) or your platform's load balancer in front for anything
  non-local; the SSE endpoint (`/runs/{id}/events`) works through
  standard proxies but needs response buffering disabled
  (`proxy_buffering off;` in nginx).
- Rollback: images are tagged by compose; `docker compose -f
  deploy/docker-compose.yml up -d --build` after a `git checkout` of the
  previous revision rebuilds and swaps. Data (DB, workspaces, cross-run
  memory) lives in the named volumes and survives.
