"""Integration tests for the task replay API endpoint (T-091)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.base import Base
from db.enums import TaskStatus, WorkerRuntimeMode
from orchestrator.execution import TaskExecutionService
from repositories import (
    TaskRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerProfile, WorkerRequest, WorkerResult


class _StaticWorker(Worker):
    """Worker double that returns a predefined result."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            status="success",
            summary="ok",
            commands_run=[],
            files_changed=[],
            artifacts=[],
        )


def _run_one_queued_task(client: TestClient) -> None:
    """Claim one queued task and execute it through the worker service."""
    service = client.app.state.task_service
    claim = service.claim_next_task(worker_id="test-worker", lease_seconds=60)
    assert claim is not None
    asyncio.run(service.run_queued_task(task_id=claim.task_id, worker_id="test-worker"))


@pytest.fixture
def session_factory():
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
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=_StaticWorker(),
        ),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = "test-shared-secret"
        yield test_client


def _create_and_complete_task(
    client: TestClient,
    *,
    task_text: str = "Fix the bug",
    repo_url: str | None = "https://github.com/example/repo",
    branch: str | None = "main",
) -> str:
    """Submit a task, run it, and return the task_id."""
    response = client.post(
        "/tasks",
        json={
            "task_text": task_text,
            "repo_url": repo_url,
            "branch": branch,
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    _run_one_queued_task(client)
    return task_id


def test_replay_returns_201_with_new_task_snapshot(
    client: TestClient,
    session_factory,
) -> None:
    """POST /tasks/{id}/replay should create a new task and return 201."""
    source_id = _create_and_complete_task(client)

    replay_response = client.post(f"/tasks/{source_id}/replay")

    assert replay_response.status_code == 201
    payload = replay_response.json()
    assert payload["task_id"] != source_id
    assert payload["task_text"] == "Fix the bug"
    assert payload["repo_url"] == "https://github.com/example/repo"
    assert payload["branch"] == "main"
    assert payload["status"] == "pending"


def test_replay_with_worker_override(
    client: TestClient,
    session_factory,
) -> None:
    """Replay with worker_override should apply the override to the new task."""
    source_id = _create_and_complete_task(client)

    replay_response = client.post(
        f"/tasks/{source_id}/replay",
        json={"worker_override": "gemini"},
    )

    assert replay_response.status_code == 201
    new_task_id = replay_response.json()["task_id"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(new_task_id)
        assert task is not None
        assert task.worker_override.value == "gemini"


def test_replay_with_worker_profile_override(
    session_factory,
) -> None:
    """Replay should accept worker_profile_override and persist it for routing."""
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=_StaticWorker(),
            enable_worker_profiles=True,
            worker_profiles={
                "codex-native-executor": WorkerProfile(
                    name="codex-native-executor",
                    worker_type="codex",
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                    capability_tags=["execution"],
                    supported_delivery_modes=["workspace", "branch", "draft_pr"],
                    permission_profile="workspace_write",
                    mutation_policy="patch_allowed",
                    self_review_policy="on_failure",
                ),
                "codex-tool-loop-executor": WorkerProfile(
                    name="codex-tool-loop-executor",
                    worker_type="codex",
                    runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
                    capability_tags=["execution"],
                    supported_delivery_modes=["workspace", "branch", "draft_pr"],
                    permission_profile="workspace_write",
                    mutation_policy="patch_allowed",
                    self_review_policy="on_failure",
                    metadata={"legacy_mode": True},
                ),
            },
        ),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )
    with TestClient(app) as profiled_client:
        profiled_client.headers["X-Webhook-Token"] = "test-shared-secret"
        source_id = _create_and_complete_task(profiled_client)

        replay_response = profiled_client.post(
            f"/tasks/{source_id}/replay",
            json={"worker_profile_override": "codex-tool-loop-executor"},
        )

        assert replay_response.status_code == 201
        new_task_id = replay_response.json()["task_id"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(new_task_id)
        assert task is not None
        assert task.constraints.get("worker_profile_override") == "codex-tool-loop-executor"


def test_replay_rejects_unknown_worker_profile_override(
    session_factory,
) -> None:
    """Replay should fail fast when worker_profile_override is not configured."""
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=_StaticWorker(),
            enable_worker_profiles=True,
            worker_profiles={
                "codex-native-executor": WorkerProfile(
                    name="codex-native-executor",
                    worker_type="codex",
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                    capability_tags=["execution"],
                    supported_delivery_modes=["workspace", "branch", "draft_pr"],
                    permission_profile="workspace_write",
                    mutation_policy="patch_allowed",
                    self_review_policy="on_failure",
                ),
            },
        ),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )
    with TestClient(app) as profiled_client:
        profiled_client.headers["X-Webhook-Token"] = "test-shared-secret"
        source_id = _create_and_complete_task(profiled_client)

        replay_response = profiled_client.post(
            f"/tasks/{source_id}/replay",
            json={"worker_profile_override": "codex-tool-loop-executor"},
        )

        assert replay_response.status_code == 422
        assert "unknown profile" in replay_response.json()["detail"].lower()


def test_replay_with_empty_body(client: TestClient) -> None:
    """Replay with an empty JSON body should replay with original parameters."""
    source_id = _create_and_complete_task(client)

    replay_response = client.post(f"/tasks/{source_id}/replay", json={})

    assert replay_response.status_code == 201
    assert replay_response.json()["task_text"] == "Fix the bug"


def test_replay_nonexistent_task_returns_404(client: TestClient) -> None:
    """Replay of a missing task should return 404."""
    replay_response = client.post("/tasks/does-not-exist/replay")

    assert replay_response.status_code == 404
    assert "not found" in replay_response.json()["detail"].lower()


def test_replay_pending_task_returns_409(client: TestClient) -> None:
    """Replay of a non-terminal task should return 409."""
    response = client.post("/tasks", json={"task_text": "Still pending"})
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    replay_response = client.post(f"/tasks/{task_id}/replay")

    assert replay_response.status_code == 409
    assert "cannot be replayed" in replay_response.json()["detail"].lower()


def test_replay_failed_task_creates_new_task(
    client: TestClient,
    session_factory,
) -> None:
    """Failed tasks should be replayable through the API."""
    source_id = _create_and_complete_task(client)
    # Force-fail the task via direct DB access
    with session_scope(session_factory) as session:
        TaskRepository(session).update_status(task_id=source_id, status=TaskStatus.FAILED)

    replay_response = client.post(f"/tasks/{source_id}/replay")

    assert replay_response.status_code == 201
    assert replay_response.json()["task_id"] != source_id
    assert replay_response.json()["task_text"] == "Fix the bug"


def test_replay_tags_provenance_in_new_task(
    client: TestClient,
    session_factory,
) -> None:
    """The replayed task should carry replayed_from provenance in constraints."""
    source_id = _create_and_complete_task(client)

    replay_response = client.post(f"/tasks/{source_id}/replay")
    assert replay_response.status_code == 201
    new_task_id = replay_response.json()["task_id"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(new_task_id)
        assert task is not None
        assert task.constraints.get("replayed_from") == [source_id]


def test_replay_unauthenticated_returns_401(session_factory) -> None:
    """Replay endpoint should reject unauthenticated requests."""
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=_StaticWorker(),
        ),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )
    with TestClient(app) as client:
        response = client.post("/tasks/some-task/replay")

    assert response.status_code == 401
