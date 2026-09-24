FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py database.py dashboard.py dashboard.html errors.py schemas.py storage.py worker.py ./
RUN uv sync --frozen --no-dev


FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    RELAY_DATABASE_URL="sqlite:////data/agent-relay.db"

COPY --from=builder /app/.venv /app/.venv
COPY main.py database.py dashboard.py dashboard.html errors.py schemas.py storage.py worker.py ./

RUN mkdir -p /data && chown -R 1000:1000 /data
USER 1000

VOLUME /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]