"""Integration tests for knowledge-base skeptical-memory endpoints (T-144)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.base import Base
from orchestrator.execution import TaskExecutionService
from repositories import create_engine_from_url, create_session_factory
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


def _create_session_and_get_user_id(client: TestClient) -> str:
    task_response = client.post(
        "/tasks",
        json={
            "task_text": "seed user for memory management",
            "session": {
                "channel": "http",
                "external_user_id": "test-user",
                "external_thread_id": "thread-memory",
            },
        },
    )
    assert task_response.status_code == 202
    sessions_response = client.get("/sessions")
    assert sessions_response.status_code == 200
    return sessions_response.json()[0]["user_id"]


def test_personal_memory_endpoints_support_crud(client: TestClient) -> None:
    """Personal memory should support list, upsert, and delete over API."""
    user_id = _create_session_and_get_user_id(client)

    upsert_response = client.put(
        "/knowledge-base/personal",
        json={
            "user_id": user_id,
            "memory_key": "communication_style",
            "value": {"style": "concise"},
            "source": "operator",
            "confidence": 0.9,
            "scope": "global",
            "requires_verification": False,
        },
    )
    assert upsert_response.status_code == 200
    upsert_payload = upsert_response.json()
    assert upsert_payload["user_id"] == user_id
    assert upsert_payload["memory_key"] == "communication_style"
    assert upsert_payload["value"] == {"style": "concise"}
    assert upsert_payload["source"] == "operator"
    assert upsert_payload["confidence"] == 0.9
    assert upsert_payload["scope"] == "global"
    assert upsert_payload["requires_verification"] is False

    list_response = client.get(f"/knowledge-base/personal?user_id={user_id}")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert len(list_payload) == 1
    assert list_payload[0]["memory_key"] == "communication_style"

    delete_response = client.delete(
        f"/knowledge-base/personal?user_id={user_id}&memory_key=communication_style"
    )
    assert delete_response.status_code == 204

    list_after_delete = client.get(f"/knowledge-base/personal?user_id={user_id}")
    assert list_after_delete.status_code == 200
    assert list_after_delete.json() == []


def test_project_memory_endpoints_support_crud(client: TestClient) -> None:
    """Project memory should support list, upsert, and delete over API."""
    repo_url = "https://github.com/natanayalo/code-agent"

    upsert_response = client.put(
        "/knowledge-base/project",
        json={
            "repo_url": repo_url,
            "memory_key": "build_command",
            "value": {"cmd": ".venv/bin/pytest"},
            "source": "sandbox_run",
            "confidence": 1.0,
            "scope": "repo",
            "requires_verification": True,
        },
    )
    assert upsert_response.status_code == 200
    upsert_payload = upsert_response.json()
    assert upsert_payload["repo_url"] == repo_url
    assert upsert_payload["memory_key"] == "build_command"
    assert upsert_payload["value"] == {"cmd": ".venv/bin/pytest"}

    list_response = client.get(f"/knowledge-base/project?repo_url={repo_url}")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert len(list_payload) == 1
    assert list_payload[0]["memory_key"] == "build_command"

    delete_response = client.delete(
        f"/knowledge-base/project?repo_url={repo_url}&memory_key=build_command"
    )
    assert delete_response.status_code == 204

    list_after_delete = client.get(f"/knowledge-base/project?repo_url={repo_url}")
    assert list_after_delete.status_code == 200
    assert list_after_delete.json() == []


def test_knowledge_base_delete_returns_not_found_when_entry_missing(client: TestClient) -> None:
    """Deleting a missing knowledge-base row should return 404."""
    personal_response = client.delete(
        "/knowledge-base/personal?user_id=user-missing&memory_key=missing"
    )
    assert personal_response.status_code == 404

    project_response = client.delete(
        "/knowledge-base/project?repo_url=https://example.com/repo&memory_key=missing"
    )
    assert project_response.status_code == 404


def test_knowledge_base_upsert_validates_confidence_bounds(client: TestClient) -> None:
    """Confidence must remain in [0.0, 1.0] per skeptical-memory policy."""
    user_id = _create_session_and_get_user_id(client)
    response = client.put(
        "/knowledge-base/personal",
        json={
            "user_id": user_id,
            "memory_key": "invalid-confidence",
            "value": {"foo": "bar"},
            "confidence": 1.1,
        },
    )
    assert response.status_code == 422
