# Agent Relay with Docker

The included `Dockerfile` builds a multi-stage image with `uv`: the builder
stage installs the locked dependencies into a `.venv`, and the runtime stage
copies that environment plus only the app modules (`main.py`, `database.py`,
`dashboard.py`, `dashboard.html`, `errors.py`, `schemas.py`, `storage.py`,
`worker.py`). The app runs as a non-root user (UID 1000).

## Run with Docker Compose

The included `compose.yaml` starts the relay and a PostgreSQL service named
`postgres` together. The relay waits for the database health check before
starting, and PostgreSQL data persists on the `postgres-data` named volume.

```bash
docker compose up -d --build
```

## Build and run the image directly

```bash
docker build -t agent-relay:local .

docker run -d --name agent-relay \
  -p 8000:8000 \
  -e RELAY_DATABASE_URL=postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay \
  agent-relay:local
```

The image defaults `RELAY_DATABASE_URL` to
`postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay`, matching the
credentials used by `compose.yaml`. Point it at any reachable PostgreSQL
instance to persist elsewhere. Without a database, the app retries startup
(`RELAY_DB_INIT_RETRIES`, default 30) until PostgreSQL accepts connections.

## Health and readiness

- `GET /health` — liveness check.
- `GET /ready` — verifies database connectivity and that the real tables exist
  (503 until the schema is reachable).
- The image ships a Docker `HEALTHCHECK` that polls `/health`; `compose.yaml`
  overrides it to poll `/ready` so orchestration knows the database is up.

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
```

## Logs and lifecycle

```bash
docker logs -f agent-relay
docker compose stop
docker compose down       # keep the database volume
docker compose down -v    # remove the database volume too
docker rm agent-relay
```

## Configuration

Pass settings with `-e` (or under `environment:` in `compose.yaml`):

```bash
docker run -d --name agent-relay \
  -p 8000:8000 \
  -e RELAY_DATABASE_URL=postgresql+psycopg://relay:relay@postgres:5432/agent_relay \
  -e RELAY_ENROLLMENT_SECRET=change-me \
  -e RELAY_LEASE_SECONDS=60 \
  -e RELAY_MAX_ATTEMPTS=5 \
  -e RELAY_RECOVERY_INTERVAL_SECONDS=5 \
  agent-relay:local
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `RELAY_DATABASE_URL` | `postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay` | SQLAlchemy PostgreSQL URL |
| `RELAY_ENROLLMENT_SECRET` | unset | Required `X-Enrollment-Secret` for registration |
| `RELAY_LEASE_SECONDS` | `60` | Claim lease duration |
| `RELAY_MAX_ATTEMPTS` | `5` | Max attempts before a task fails |
| `RELAY_RECOVERY_INTERVAL_SECONDS` | `5` | Lease recovery pass interval |
| `RELAY_MAX_BODY_BYTES` | `262144` | Max request body size |

## Dashboard

The token-based local dashboard is served at both `/` and `/dashboard`.

## Worker

The relay server runs via `CMD ["uvicorn", "main:app", ...]`. The
deterministic uppercase worker is designed to run on its own machines
(see `README.md`); to run it from this image instead, override the command:

```bash
docker run --rm agent-relay:local \
  python main.py worker \
  --base-url http://<host>:8000 \
  --name uppercase \
  --worker-id container-1
```