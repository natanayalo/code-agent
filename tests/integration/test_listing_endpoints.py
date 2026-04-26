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
    session_scope,
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
    # Verify summary view lacks full history (T-131 optimization)
    assert "timeline" not in payload[0]
    assert "latest_run" not in payload[0]
    assert "latest_run_id" in payload[0]


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


def test_get_task_returns_detailed_snapshot(client: TestClient) -> None:
    """GET /tasks/{id} should return a full task snapshot with timeline."""
    resp = client.post(
        "/tasks",
        json={
            "task_text": "detailed task",
            "session": {
                "channel": "http",
                "external_user_id": "test-user",
                "external_thread_id": "thread-detail",
            },
        },
    )
    task_id = resp.json()["task_id"]

    response = client.get(f"/tasks/{task_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == task_id
    assert "timeline" in payload
    # In StaticWorker tests, there might be events created during submission
    assert isinstance(payload["timeline"], list)


def test_list_tasks_includes_approval_context(client: TestClient, session_factory) -> None:
    """GET /tasks should include approval status, type, reason, and requested permission (T-134)."""
    with session_scope(session_factory) as session:
        # Create a task with approval constraints
        from datetime import datetime

        from db.enums import TaskStatus, WorkerRunStatus, WorkerType
        from db.models import Task, WorkerRun

        task = Task(
            session_id="session-1",
            task_text="approval test task",
            status=TaskStatus.FAILED,  # Checkpoint pauses usually mark task as failed
            constraints={
                "approval": {
                    "status": "pending",
                    "approval_type": "permission_escalation",
                    "reason": "Dangerous command requested",
                }
            },
        )
        session.add(task)
        session.flush()

        # Create a run for it with requested permission
        run = WorkerRun(
            task_id=task.id,
            worker_type=WorkerType.CODEX,
            started_at=datetime.utcnow(),
            status=WorkerRunStatus.FAILURE,
            requested_permission="dangerous_shell",
        )
        session.add(run)
        session.commit()

    response = client.get("/tasks")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) >= 1

    task_snapshot = next(t for t in payload if t["task_text"] == "approval test task")
    assert task_snapshot["approval_status"] == "pending"
    assert task_snapshot["approval_type"] == "permission_escalation"
    assert task_snapshot["approval_reason"] == "Dangerous command requested"
    assert task_snapshot["latest_run_requested_permission"] == "dangerous_shell"
