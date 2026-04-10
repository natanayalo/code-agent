"""Integration tests for the minimal task submission/status API."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.base import Base
from db.enums import ArtifactType, TaskStatus, WorkerRunStatus
from orchestrator.execution import TaskExecutionService
from repositories import (
    ArtifactRepository,
    TaskRepository,
    WorkerRunRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


class StaticWorker(Worker):
    """Worker double that returns a predefined result and records requests."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


@pytest.fixture
def session_factory():
    """Create a SQLite-backed session factory for task endpoint tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    """Provide a test client with the execution-path task service configured."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="Created note.txt and retained the workspace for inspection.",
            budget_usage={"iterations_used": 2, "tool_calls_used": 1},
            commands_run=[
                {
                    "command": "printf 'done\\n' > note.txt",
                    "exit_code": 0,
                    "duration_seconds": 0.1,
                    "stdout_artifact_uri": "artifacts/stdout.log",
                    "stderr_artifact_uri": "artifacts/stderr.log",
                }
            ],
            files_changed=["note.txt"],
            artifacts=[
                {
                    "name": "workspace",
                    "uri": "/tmp/workspace-task-44-1234",
                    "artifact_type": "workspace",
                }
            ],
            next_action_hint="inspect_workspace_artifacts",
        )
    )
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=worker,
        ),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )
    app.state.test_worker = worker
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = "test-shared-secret"
        yield test_client


def test_submit_task_persists_execution_path_and_allows_polling(
    client: TestClient, session_factory
) -> None:
    """Submitting a task should return a pollable snapshot and persist the eventual result."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Create a note and report the result",
            "repo_url": "https://github.com/natanayalo/code-agent",
            "branch": "master",
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-44",
                "display_name": "HTTP Test User",
            },
        },
    )

    assert response.status_code == 202
    payload = response.json()
    task_id = payload["task_id"]

    assert payload["status"] == "pending"
    assert payload["chosen_worker"] is None
    assert payload["route_reason"] is None
    assert payload["latest_run"] is None

    get_response = client.get(f"/tasks/{task_id}")
    latest_run = get_response.json()["latest_run"]

    assert get_response.status_code == 200
    assert get_response.json()["task_id"] == task_id
    assert get_response.json()["status"] == "completed"
    assert get_response.json()["chosen_worker"] == "codex"
    assert get_response.json()["route_reason"] == "cheap_mechanical_change"
    assert get_response.json()["latest_run"]["summary"] == (
        "Created note.txt and retained the workspace for inspection."
    )
    assert latest_run["status"] == "success"
    assert latest_run["session_id"] == payload["session_id"]
    assert latest_run["worker_type"] == "codex"
    assert latest_run["workspace_id"] == "workspace-task-44-1234"
    assert latest_run["budget_usage"] == {"iterations_used": 2, "tool_calls_used": 1}
    assert latest_run["verifier_outcome"]["status"] == "warning"
    assert latest_run["files_changed_count"] == 1
    assert latest_run["commands_run"] == [
        {
            "command": "printf 'done\\n' > note.txt",
            "exit_code": 0,
            "duration_seconds": 0.1,
            "stdout_artifact_uri": "artifacts/stdout.log",
            "stderr_artifact_uri": "artifacts/stderr.log",
        }
    ]
    assert latest_run["artifact_index"] == [
        {
            "name": "workspace",
            "uri": "/tmp/workspace-task-44-1234",
            "artifact_type": "workspace",
        }
    ]
    assert latest_run["artifacts"][0]["artifact_type"] == "workspace"
    assert latest_run["artifacts"][0]["name"] == "workspace"

    worker = client.app.state.test_worker
    assert len(worker.requests) == 1
    assert worker.requests[0].session_id == payload["session_id"]
    assert worker.requests[0].repo_url == "https://github.com/natanayalo/code-agent"
    assert worker.requests[0].branch == "master"

    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        worker_run_repo = WorkerRunRepository(session)
        artifact_repo = ArtifactRepository(session)

        task = task_repo.get(task_id)
        assert task is not None
        assert task.status is TaskStatus.COMPLETED
        assert task.chosen_worker.value == "codex"

        worker_runs = worker_run_repo.list_by_task(task_id)
        assert len(worker_runs) == 1
        worker_run = worker_runs[0]
        assert worker_run.status is WorkerRunStatus.SUCCESS
        assert worker_run.session_id == payload["session_id"]
        assert worker_run.workspace_id == "workspace-task-44-1234"
        assert worker_run.budget_usage == {"iterations_used": 2, "tool_calls_used": 1}
        assert worker_run.verifier_outcome["status"] == "warning"
        assert worker_run.files_changed_count == 1

        artifacts = artifact_repo.list_by_run(worker_run.id)
        assert len(artifacts) == 1
        assert artifacts[0].artifact_type is ArtifactType.WORKSPACE


def test_task_routes_require_a_configured_task_service() -> None:
    """The default app should fail clearly until the task service is wired in."""
    app = create_app()

    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            headers={"X-Webhook-Token": "test-shared-secret"},
            json={"task_text": "Run the task API"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Task execution service is not configured for this app instance."
    }


def test_get_task_returns_not_found_for_unknown_task(client: TestClient) -> None:
    """Polling a missing task id should return a 404."""
    response = client.get("/tasks/task-missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task 'task-missing' was not found."}


@pytest.mark.parametrize(
    "callback_url",
    [
        "ftp://callbacks.example.com/status",
        "http://localhost/callback",
        "http://127.0.0.1/callback",
        "http://192.168.1.10/callback",
    ],
)
def test_submit_task_rejects_unsafe_callback_urls(
    client: TestClient,
    callback_url: str,
) -> None:
    """Direct task submissions should reject callback URLs that are obvious SSRF targets."""
    response = client.post(
        "/tasks",
        headers={},
        json={
            "task_text": "Create a note and report the result",
            "callback_url": callback_url,
        },
    )

    assert response.status_code == 422


def test_submit_task_rejects_hostname_callback_urls_resolving_to_private_addresses(
    client: TestClient,
    monkeypatch,
) -> None:
    """Hostname callback targets should be rejected when DNS resolves to private IPs."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        assert host == "callbacks.example.com"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.10", port))
        ]

    monkeypatch.setattr("orchestrator.execution.socket.getaddrinfo", fake_getaddrinfo)

    response = client.post(
        "/tasks",
        headers={},
        json={
            "task_text": "Create a note and report the result",
            "callback_url": "https://callbacks.example.com/status",
        },
    )

    assert response.status_code == 422


def test_task_routes_reject_missing_auth_header(session_factory) -> None:
    """Task routes should reject unauthenticated requests before processing."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="ok",
            budget_usage={},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )
    )
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )

    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_text": "Run the task API"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing X-Webhook-Token header."}
    assert worker.requests == []


def test_task_routes_reject_invalid_auth_header(session_factory) -> None:
    """Task routes should reject incorrect shared-secret headers."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="ok",
            budget_usage={},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )
    )
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )

    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            headers={"X-Webhook-Token": "wrong-secret"},
            json={"task_text": "Run the task API"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid API authentication secret."}
    assert worker.requests == []


def test_task_routes_fail_closed_when_service_is_injected_without_auth_config(
    session_factory,
) -> None:
    """Injected task services should still fail closed if auth config is omitted."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="ok",
            budget_usage={},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )
    )
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
    )

    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_text": "Run the task API"})

    assert response.status_code == 500
    assert response.json() == {
        "detail": "API authentication is not configured for this app instance."
    }
    assert worker.requests == []
