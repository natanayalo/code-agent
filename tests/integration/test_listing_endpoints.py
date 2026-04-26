"""Integration tests for task and session listing endpoints."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.base import Base
from orchestrator.execution import TaskExecutionService
from repositories import (
    create_engine_from_url,
    create_session_factory,
)
from workers import Worker, WorkerRequest, WorkerResult


class StaticWorker(Worker):
    """Worker double for tests."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            status="success",
            summary="ok",
            budget_usage={},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )


@pytest.fixture
def session_factory():
    """Create a SQLite-backed session factory."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    """Provide a test client."""
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=StaticWorker(),
        ),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = "test-shared-secret"
        yield test_client


def test_list_tasks_returns_paginated_tasks(client: TestClient) -> None:
    """GET /tasks should return a list of task snapshots."""
    # Create 5 tasks
    for i in range(5):
        client.post(
            "/tasks",
            json={
                "task_text": f"task {i}",
                "session": {
                    "channel": "http",
                    "external_user_id": "test-user",
                    "external_thread_id": f"thread-{i}",
                },
            },
        )

    response = client.get("/tasks?limit=3")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 3
    assert payload[0]["task_text"] == "task 4"  # Descending order


def test_list_tasks_filters_by_session_and_status(client: TestClient) -> None:
    """GET /tasks should support session_id and status filters."""
    # Create tasks in different sessions and with different statuses
    resp1 = client.post(
        "/tasks",
        json={
            "task_text": "task 1",
            "session": {
                "channel": "http",
                "external_user_id": "test-user",
                "external_thread_id": "thread-1",
            },
        },
    )
    session_id = resp1.json()["session_id"]

    client.post(
        "/tasks",
        json={
            "task_text": "task 2",
            "session": {
                "channel": "http",
                "external_user_id": "test-user",
                "external_thread_id": "thread-2",
            },
        },
    )

    # Filter by session
    response = client.get(f"/tasks?session_id={session_id}")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["task_text"] == "task 1"

    # Filter by status
    response = client.get("/tasks?status_filter=pending")
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_list_sessions_returns_paginated_sessions(client: TestClient) -> None:
    """GET /sessions should return a list of session snapshots."""
    # Create 5 sessions by submitting tasks
    for i in range(5):
        client.post(
            "/tasks",
            json={
                "task_text": f"task {i}",
                "session": {
                    "channel": "http",
                    "external_user_id": "test-user",
                    "external_thread_id": f"thread-{i}",
                },
            },
        )

    response = client.get("/sessions?limit=3")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 3
    assert payload[0]["external_thread_id"] == "thread-4"


def test_get_session_returns_snapshot(client: TestClient) -> None:
    """GET /sessions/{id} should return a session snapshot."""
    resp = client.post(
        "/tasks",
        json={
            "task_text": "task 1",
            "session": {
                "channel": "http",
                "external_user_id": "test-user",
                "external_thread_id": "thread-1",
            },
        },
    )
    session_id = resp.json()["session_id"]

    response = client.get(f"/sessions/{session_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == session_id
    assert payload["external_thread_id"] == "thread-1"


def test_get_session_returns_404_for_missing_session(client: TestClient) -> None:
    """GET /sessions/{id} should return 404 for unknown session."""
    response = client.get("/sessions/session-missing")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
