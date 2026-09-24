# Agent Relay (PostgreSQL)

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

## Run it with Docker Compose

The included `compose.yaml` runs the relay (built from the `Dockerfile`) and a
`postgres` service together, with the API waiting for the database to become
healthy:

```bash
docker compose up -d --build
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. `GET
/health` is a liveness check and `GET /ready` verifies PostgreSQL connectivity
and that the real tables exist. Let compose use a fresh checkout with:

```bash
docker compose down -v   # removes the postgres-data volume
```

## Run it locally (without Docker)

Start PostgreSQL with the defaults Compose uses, or point
`RELAY_DATABASE_URL` at any PostgreSQL instance:

```bash
docker compose up -d postgres
uv sync
uv run uvicorn main:app --reload
```

The default database URL is
`postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay`; set
`RELAY_DATABASE_URL` to override it.

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains the SQLAlchemy models and the PostgreSQL engine.
`storage.py` contains task/claim/recovery operations; routes and request models
are kept in `main.py` and `schemas.py`. Concurrent claims are coordinated with
`SELECT ... FOR UPDATE SKIP LOCKED` so several API processes can claim from one
inbox without locking each other out; heartbeats, terminal submissions, and
lease recovery lock the affected attempt row with `FOR UPDATE` so a race has
one consistent outcome.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
docker compose up -d postgres   # start the database the tests run against
uv run pytest -q
```

Tests default to a scratch database `agent_relay_test` in the compose Postgres
instance (created automatically if missing), so they do not reset your dev
server's data. The fixture drops and recreates all tables on whatever
`RELAY_DATABASE_URL` points at, so stop the dev server first or set
`RELAY_DATABASE_URL` to a scratch database before running tests against another
PostgreSQL instance.