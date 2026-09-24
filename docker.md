# Agent Relay with Docker

The included `Dockerfile` builds a multi-stage image with `uv`: the builder
stage installs the locked dependencies into a `.venv`, and the runtime stage
copies that environment plus only the app modules (`main.py`, `database.py`,
`dashboard.py`, `dashboard.html`, `errors.py`, `schemas.py`, `storage.py`,
`worker.py`). The app runs as a non-root user (UID 1000).

## Build

```bash
docker build -t agent-relay:local .
```

## Run

```bash
docker run -d --name agent-relay \
  -p 8000:8000 \
  -v agent-relay-data:/data \
  agent-relay:local
```

The image defaults `RELAY_DATABASE_URL` to `sqlite:////data/agent-relay.db`, so
the SQLite database lives on the `agent-relay-data` named volume and survives
container replacement. Override `RELAY_DATABASE_URL` (e.g. with a PostgreSQL
URL) to persist elsewhere.

## Health and readiness

- `GET /health` — liveness check.
- `GET /ready` — verifies database connectivity and that the real tables exist
  (503 after a wiped volume).
- The image also ships a Docker `HEALTHCHECK` that polls `/health`.

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
```

## Logs and lifecycle

```bash
docker logs -f agent-relay
docker stop agent-relay
docker rm agent-relay
```

## Configuration

Pass settings with `-e`:

```bash
docker run -d --name agent-relay \
  -p 8000:8000 \
  -v agent-relay-data:/data \
  -e RELAY_ENROLLMENT_SECRET=change-me \
  -e RELAY_LEASE_SECONDS=60 \
  -e RELAY_MAX_ATTEMPTS=5 \
  -e RELAY_RECOVERY_INTERVAL_SECONDS=5 \
  agent-relay:local
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `RELAY_DATABASE_URL` | `sqlite:////data/agent-relay.db` | SQLAlchemy database URL |
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