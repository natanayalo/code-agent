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
        auth_config=ApiAuthConfig(shared_secret=("a" * 32)),  # gitleaks:allow
    )
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = (
            "a" * 32  # gitleaks:allow
        )
        yield test_client


def test_personal_memory_endpoints_support_crud(client: TestClient) -> None:
    """Personal memory should support list, upsert, and delete over API."""
    upsert_response = client.put(
        "/knowledge-base/personal",
        json={
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
    assert "user_id" not in upsert_payload
    assert upsert_payload["memory_key"] == "communication_style"
    assert upsert_payload["value"] == {"style": "concise"}
    assert upsert_payload["source"] == "operator"
    assert upsert_payload["confidence"] == 0.9
    assert upsert_payload["scope"] == "global"
    assert upsert_payload["requires_verification"] is False

    list_response = client.get("/knowledge-base/personal")
    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert len(list_payload) == 1
    assert list_payload[0]["memory_key"] == "communication_style"

    delete_response = client.delete("/knowledge-base/personal?memory_key=communication_style")
    assert delete_response.status_code == 204

    list_after_delete = client.get("/knowledge-base/personal")
    assert list_after_delete.status_code == 200
    assert list_after_delete.json() == []


def test_personal_memory_list_supports_global_inventory(client: TestClient) -> None:
    """Personal memory listing should not require a user scope."""
    response = client.get("/knowledge-base/personal")
    assert response.status_code == 200
    assert response.json() == []


def test_personal_memory_search_supports_global_lookup(client: TestClient) -> None:
    """Personal memory search should return global entries and ignore blank queries."""
    client.put(
        "/knowledge-base/personal",
        json={
            "memory_key": "communication_style",
            "value": {"style": "concise"},
        },
    )

    search_response = client.get("/knowledge-base/personal/search?q=concise&limit=10")
    assert search_response.status_code == 200
    assert [entry["memory_key"] for entry in search_response.json()] == ["communication_style"]
    assert search_response.json()[0]["headline"] is None

    empty_response = client.get("/knowledge-base/personal/search?q=   ")
    assert empty_response.status_code == 200
    assert empty_response.json() == []


def test_knowledge_base_stats_returns_exact_counts_and_updates_after_delete(
    client: TestClient,
) -> None:
    """Knowledge-base stats should report scoped and global memory inventory counts."""
    repo_url = "https://github.com/natanayalo/code-agent"
    other_repo_url = "https://github.com/natanayalo/other"

    client.put(
        "/knowledge-base/personal",
        json={
            "memory_key": "style",
            "value": {"style": "concise"},
            "requires_verification": True,
        },
    )
    client.put(
        "/knowledge-base/personal",
        json={
            "memory_key": "editor",
            "value": {"theme": "dark"},
            "requires_verification": False,
        },
    )
    client.put(
        "/knowledge-base/project",
        json={
            "repo_url": repo_url,
            "memory_key": "build_command",
            "value": {"cmd": ".venv/bin/pytest"},
            "requires_verification": True,
        },
    )
    client.put(
        "/knowledge-base/project",
        json={
            "repo_url": other_repo_url,
            "memory_key": "lint_command",
            "value": {"cmd": "npm run lint"},
            "requires_verification": False,
        },
    )

    response = client.get(f"/knowledge-base/stats?repo_url={repo_url}")
    assert response.status_code == 200
    assert response.json() == {
        "personal": {"total": 2, "requires_verification": 1},
        "project": {"total": 1, "requires_verification": 1},
        "project_global": {"total": 2, "requires_verification": 1},
    }

    delete_response = client.delete(
        f"/knowledge-base/project?repo_url={repo_url}&memory_key=build_command"
    )
    assert delete_response.status_code == 204

    updated_response = client.get(f"/knowledge-base/stats?repo_url={repo_url}")
    assert updated_response.status_code == 200
    assert updated_response.json() == {
        "personal": {"total": 2, "requires_verification": 1},
        "project": {"total": 0, "requires_verification": 0},
        "project_global": {"total": 1, "requires_verification": 0},
    }


def test_knowledge_base_stats_allows_blank_scopes(client: TestClient) -> None:
    """Blank project scopes should return null project counts and exact personal counts."""
    response = client.get("/knowledge-base/stats")

    assert response.status_code == 200
    assert response.json() == {
        "personal": {"total": 0, "requires_verification": 0},
        "project": None,
        "project_global": {"total": 0, "requires_verification": 0},
    }

    blank_response = client.get("/knowledge-base/stats?repo_url=")

    assert blank_response.status_code == 200
    assert blank_response.json() == {
        "personal": {"total": 0, "requires_verification": 0},
        "project": None,
        "project_global": {"total": 0, "requires_verification": 0},
    }


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
    personal_response = client.delete("/knowledge-base/personal?memory_key=missing")
    assert personal_response.status_code == 404

    project_response = client.delete(
        "/knowledge-base/project?repo_url=https://example.com/repo&memory_key=missing"
    )
    assert project_response.status_code == 404


def test_knowledge_base_upsert_validates_confidence_bounds(client: TestClient) -> None:
    """Confidence must remain in [0.0, 1.0] per skeptical-memory policy."""
    response = client.put(
        "/knowledge-base/personal",
        json={
            "memory_key": "invalid-confidence",
            "value": {"foo": "bar"},
            "confidence": 1.1,
        },
    )
    assert response.status_code == 422


def test_project_memory_search_requires_repo_and_validates_limit(client: TestClient) -> None:
    """Project memory search should validate inputs and scope lookups by repo."""
    repo_url = "https://github.com/natanayalo/code-agent"
    client.put(
        "/knowledge-base/project",
        json={
            "repo_url": repo_url,
            "memory_key": "build_command",
            "value": {"cmd": ".venv/bin/pytest"},
        },
    )

    response = client.get(f"/knowledge-base/project/search?repo_url={repo_url}&q=pytest")
    assert response.status_code == 200
    assert [entry["memory_key"] for entry in response.json()] == ["build_command"]
    assert response.json()[0]["headline"] is None

    missing_repo = client.get("/knowledge-base/project/search?q=pytest")
    assert missing_repo.status_code == 422

    invalid_limit = client.get(
        f"/knowledge-base/project/search?repo_url={repo_url}&q=pytest&limit=101"
    )
    assert invalid_limit.status_code == 422
