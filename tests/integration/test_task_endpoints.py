"""Integration tests for the minimal task submission/status API."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

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
            commands_run=[
                {
                    "command": "printf 'done\\n' > note.txt",
                    "exit_code": 0,
                    "duration_seconds": 0.1,
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
        )
    )
    app.state.test_worker = worker
    with TestClient(app) as test_client:
        yield test_client


def test_submit_task_persists_execution_path_and_allows_polling(
    client: TestClient, session_factory
) -> None:
    """Submitting a task should persist task, run, and artifact state for later polling."""
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

    assert response.status_code == 201
    payload = response.json()
    task_id = payload["task_id"]
    latest_run = payload["latest_run"]

    assert payload["status"] == "completed"
    assert payload["chosen_worker"] == "codex"
    assert payload["route_reason"] == "implementation_default"
    assert latest_run["status"] == "success"
    assert latest_run["worker_type"] == "codex"
    assert latest_run["workspace_id"] == "workspace-task-44-1234"
    assert latest_run["files_changed_count"] == 1
    assert latest_run["artifact_index"] == [
        {
            "name": "workspace",
            "uri": "/tmp/workspace-task-44-1234",
            "artifact_type": "workspace",
        }
    ]
    assert latest_run["artifacts"][0]["artifact_type"] == "workspace"
    assert latest_run["artifacts"][0]["name"] == "workspace"

    get_response = client.get(f"/tasks/{task_id}")

    assert get_response.status_code == 200
    assert get_response.json()["task_id"] == task_id
    assert get_response.json()["latest_run"]["summary"] == (
        "Created note.txt and retained the workspace for inspection."
    )

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
        assert worker_run.workspace_id == "workspace-task-44-1234"
        assert worker_run.files_changed_count == 1

        artifacts = artifact_repo.list_by_run(worker_run.id)
        assert len(artifacts) == 1
        assert artifacts[0].artifact_type is ArtifactType.WORKSPACE


def test_task_routes_require_a_configured_task_service() -> None:
    """The default app should fail clearly until the task service is wired in."""
    app = create_app()

    with TestClient(app) as client:
        response = client.post("/tasks", json={"task_text": "Run the task API"})

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Task execution service is not configured for this app instance."
    }


def test_get_task_returns_not_found_for_unknown_task(client: TestClient) -> None:
    """Polling a missing task id should return a 404."""
    response = client.get("/tasks/task-missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task 'task-missing' was not found."}
