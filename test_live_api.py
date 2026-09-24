"""Live HTTP integration tests for the running Agent Relay API.

These tests exercise the real service over HTTP (never the in-process
TestClient) against the real PostgreSQL database. They only register fresh
agents and create fresh tasks; they never drop or truncate the real tables.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
PROBE_WINDOW_SECONDS = 5.0


def _live_base_url() -> str:
    return os.environ.get("RELAY_BASE_URL", DEFAULT_BASE_URL)


def _live_ready(url: str) -> str | None:
    """Return None when /ready is 200, else a message describing the failure."""
    deadline = time.monotonic() + PROBE_WINDOW_SECONDS
    last_reason = "no response"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/ready", timeout=2)
            if response.status_code == 200:
                return None
            last_reason = f"GET /ready returned HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_reason = f"connection error: {exc}"
        time.sleep(0.25)
    return last_reason


@pytest.fixture(scope="session", autouse=True)
def live_api_is_reachable() -> None:
    url = _live_base_url()
    reason = _live_ready(url)
    if reason is not None:
        pytest.skip(f"live Agent Relay API at {url} is not reachable: {reason}")


@pytest.fixture(scope="session")
def live_client(live_api_is_reachable: None) -> Iterator[httpx.Client]:
    del live_api_is_reachable
    with httpx.Client(base_url=_live_base_url(), timeout=10) as client:
        yield client


def live_register(client: httpx.Client, prefix: str) -> tuple[str, str]:
    headers: dict[str, str] = {}
    secret = os.environ.get("RELAY_ENROLLMENT_SECRET") or os.environ.get("ENROLLMENT_SECRET")
    if secret is not None:
        headers["X-Enrollment-Secret"] = secret
    response = client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": f"{prefix}-{uuid.uuid4().hex[:8]}"},
    )
    assert response.status_code == 201
    agent = response.json()
    assert "agent_id" in agent and "token" in agent
    return agent["agent_id"], agent["token"]


def test_live_acceptance_one(live_client: httpx.Client) -> None:
    sender_id, sender_token = live_register(live_client, "alice")
    recipient_id, recipient_token = live_register(live_client, "uppercase")
    sender_headers = {"Authorization": f"Bearer {sender_token}"}
    recipient_headers = {"Authorization": f"Bearer {recipient_token}"}

    input_text = f"hello live relay {uuid.uuid4().hex[:8]}"
    idempotency_key = {"Idempotency-Key": f"live-{uuid.uuid4().hex[:8]}"}
    payload = {"to": recipient_id, "input": input_text}

    sent = live_client.post("/api/v1/tasks", headers={**sender_headers, **idempotency_key}, json=payload)
    assert sent.status_code == 201
    task = sent.json()
    assert task["status"] == "queued"
    task_id = task["task_id"]

    duplicate = live_client.post("/api/v1/tasks", headers={**sender_headers, **idempotency_key}, json=payload)
    assert duplicate.status_code == 201
    assert duplicate.json()["task_id"] == task_id

    conflict = live_client.post(
        "/api/v1/tasks",
        headers={**sender_headers, **idempotency_key},
        json={"to": recipient_id, "input": f"{input_text}!"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    claim = live_client.post(
        "/api/v1/tasks/claim",
        headers=recipient_headers,
        json={"worker_id": "live-worker", "wait_seconds": 0},
    )
    assert claim.status_code == 200
    claim_data = claim.json()
    assert claim_data["task_id"] == task_id
    assert claim_data["from"] == sender_id
    assert claim_data["input"] == input_text
    assert claim_data["attempt"] == 1
    assert "claim_token" in claim_data

    output = input_text.upper()
    complete_body = {"claim_token": claim_data["claim_token"], "output": output}
    completed = live_client.post(f"/api/v1/tasks/{task_id}/complete", headers=recipient_headers, json=complete_body)
    assert completed.status_code == 200
    assert completed.json() == {"task_id": task_id, "status": "completed"}

    task_get = live_client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers)
    assert task_get.status_code == 200
    task_detail = task_get.json()
    assert task_detail["task_id"] == task_id
    assert task_detail["status"] == "completed"
    assert task_detail["output"] == output
    assert task_detail["attempt_count"] == 1

    retry = live_client.post(f"/api/v1/tasks/{task_id}/complete", headers=recipient_headers, json=complete_body)
    assert retry.status_code == 200
    assert retry.json() == {"task_id": task_id, "status": "completed"}

    attempts = live_client.get(f"/api/v1/tasks/{task_id}/attempts", headers=recipient_headers).json()
    assert len(attempts["items"]) == 1
    assert attempts["items"][0]["outcome"] == "completed"
    assert "claim_token" not in attempts["items"][0]

    sent_list = live_client.get("/api/v1/tasks", headers=sender_headers, params={"direction": "sent"}).json()
    assert any(item["task_id"] == task_id and item["status"] == "completed" for item in sent_list["items"])
    received_list = live_client.get("/api/v1/tasks", headers=recipient_headers, params={"direction": "received"}).json()
    assert any(item["task_id"] == task_id for item in received_list["items"])

    agents = live_client.get("/api/v1/agents", headers=sender_headers).json()
    agent_ids = {item["agent_id"] for item in agents["items"]}
    assert sender_id in agent_ids
    assert recipient_id in agent_ids

    dashboard = live_client.get("/")
    assert dashboard.status_code == 200
    assert "sessionStorage" in dashboard.text

    no_credentials = live_client.get("/api/v1/agents")
    assert no_credentials.status_code == 401
    error = no_credentials.json()["error"]
    assert "code" in error and "message" in error