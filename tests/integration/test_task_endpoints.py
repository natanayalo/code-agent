"""Integration tests for the minimal task submission/status API."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.auth import (
    API_SHARED_SECRET_HEADER,
    DASHBOARD_COOKIE_NAME,
    ApiAuthConfig,
    create_dashboard_token,
)
from apps.api.main import create_app
from db.base import Base
from db.enums import ArtifactType, TaskStatus, WorkerRunStatus, WorkerRuntimeMode
from orchestrator.execution import TaskExecutionService
from repositories import (
    ArtifactRepository,
    TaskRepository,
    WorkerRunRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerProfile, WorkerRequest, WorkerResult


class StaticWorker(Worker):
    """Worker double that returns a predefined result and records requests."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


def _run_one_queued_task(client: TestClient) -> None:
    """Claim one queued task and execute it through the worker service."""
    service = client.app.state.task_service
    claim = service.claim_next_task(worker_id="test-worker", lease_seconds=60)
    assert claim is not None
    asyncio.run(service.run_queued_task(task_id=claim.task_id, worker_id="test-worker"))


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
            checkpoint_path="test_checkpoints.sqlite",
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
    assert payload["task_spec"]["goal"] == "Create a note and report the result"
    assert payload["task_spec"]["task_type"] == "feature"
    assert payload["task_spec"]["risk_level"] == "low"
    assert payload["latest_run"] is None

    _run_one_queued_task(client)

    get_response = client.get(f"/tasks/{task_id}")
    latest_run = get_response.json()["latest_run"]

    assert get_response.status_code == 200
    assert get_response.json()["task_id"] == task_id
    assert get_response.json()["status"] == "completed"
    assert get_response.json()["chosen_worker"] == "codex"
    assert get_response.json()["route_reason"] == "cheap_mechanical_change"
    assert get_response.json()["task_spec"]["delivery_mode"] == "workspace"
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
    assert latest_run["files_changed"] == ["note.txt"]
    assert len(latest_run["commands_run"]) == 1
    assert latest_run["commands_run"][0]["command"] == "printf 'done\\n' > note.txt"
    assert latest_run["commands_run"][0]["exit_code"] == 0
    assert latest_run["commands_run"][0]["duration_seconds"] == 0.1
    assert latest_run["commands_run"][0]["stdout_artifact_uri"] == "artifacts/stdout.log"
    assert latest_run["commands_run"][0]["stderr_artifact_uri"] == "artifacts/stderr.log"
    assert "id" in latest_run["commands_run"][0]
    assert len(latest_run["artifact_index"]) == 1
    assert latest_run["artifact_index"][0]["name"] == "workspace"
    assert latest_run["artifact_index"][0]["uri"] == "/tmp/workspace-task-44-1234"
    assert latest_run["artifact_index"][0]["artifact_type"] == "workspace"
    assert "id" in latest_run["artifact_index"][0]
    assert latest_run["artifacts"][0]["artifact_type"] == "workspace"
    assert latest_run["artifacts"][0]["name"] == "workspace"

    worker = client.app.state.test_worker
    assert len(worker.requests) == 1
    assert worker.requests[0].session_id == payload["session_id"]
    assert worker.requests[0].repo_url == "https://github.com/natanayalo/code-agent"
    assert worker.requests[0].branch == "master"
    assert worker.requests[0].task_spec is not None
    assert worker.requests[0].task_spec["goal"] == "Create a note and report the result"

    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        worker_run_repo = WorkerRunRepository(session)
        artifact_repo = ArtifactRepository(session)

        task = task_repo.get(task_id)
        assert task is not None
        assert task.status is TaskStatus.COMPLETED
        assert task.chosen_worker.value == "codex"
        assert task.task_spec is not None
        assert task.task_spec["goal"] == "Create a note and report the result"

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


def test_task_endpoints_expose_profile_and_runtime_metadata_when_profile_routing_is_enabled(
    session_factory,
) -> None:
    """Profile-aware routing should surface selected profile/runtime on task and run snapshots."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="Applied minimal change with profiled routing metadata.",
            files_changed=["note.txt"],
            artifacts=[
                {
                    "name": "workspace",
                    "uri": "/tmp/workspace-task-profiled-1",
                    "artifact_type": "workspace",
                }
            ],
        )
    )
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=worker,
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
                )
            },
        ),
        auth_config=ApiAuthConfig(shared_secret="test-shared-secret"),
    )
    app.state.test_worker = worker

    with TestClient(app) as profiled_client:
        profiled_client.headers["X-Webhook-Token"] = "test-shared-secret"
        response = profiled_client.post(
            "/tasks",
            json={
                "task_text": "Apply a profiled codex native task",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "session": {
                    "channel": "http",
                    "external_user_id": "http:test-user-profiled",
                    "external_thread_id": "thread-profiled",
                },
            },
        )

        assert response.status_code == 202
        task_id = response.json()["task_id"]
        _run_one_queued_task(profiled_client)

        get_response = profiled_client.get(f"/tasks/{task_id}")
        assert get_response.status_code == 200
        payload = get_response.json()
        latest_run = payload["latest_run"]

        assert payload["chosen_worker"] == "codex"
        assert payload["chosen_profile"] == "codex-native-executor"
        assert payload["runtime_mode"] == "native_agent"
        assert latest_run["worker_type"] == "codex"
        assert latest_run["worker_profile"] == "codex-native-executor"
        assert latest_run["runtime_mode"] == "native_agent"
        assert len(worker.requests) == 1
        assert worker.requests[0].worker_profile == "codex-native-executor"
        assert worker.requests[0].runtime_mode == "native_agent"

        with session_scope(session_factory) as session:
            task_repo = TaskRepository(session)
            worker_run_repo = WorkerRunRepository(session)

            task = task_repo.get(task_id)
            assert task is not None
            assert task.chosen_profile == "codex-native-executor"
            assert task.runtime_mode is WorkerRuntimeMode.NATIVE_AGENT

            worker_runs = worker_run_repo.list_by_task(task_id)
            assert len(worker_runs) == 1
            worker_run = worker_runs[0]
            assert worker_run.worker_profile == "codex-native-executor"
            assert worker_run.runtime_mode is WorkerRuntimeMode.NATIVE_AGENT


def test_task_endpoints_accept_worker_profile_override_for_explicit_legacy_opt_in(
    session_factory,
) -> None:
    """Explicit worker_profile_override should pin routing to the requested legacy profile."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="Applied change through explicit legacy profile override.",
            files_changed=["note.txt"],
            artifacts=[
                {
                    "name": "workspace",
                    "uri": "/tmp/workspace-task-profiled-legacy",
                    "artifact_type": "workspace",
                }
            ],
        )
    )
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=worker,
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
    app.state.test_worker = worker

    with TestClient(app) as profiled_client:
        profiled_client.headers["X-Webhook-Token"] = "test-shared-secret"
        response = profiled_client.post(
            "/tasks",
            json={
                "task_text": "Run with explicit codex tool-loop legacy profile",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "worker_profile_override": "codex-tool-loop-executor",
                "session": {
                    "channel": "http",
                    "external_user_id": "http:test-user-profiled-legacy",
                    "external_thread_id": "thread-profiled-legacy",
                },
            },
        )

        assert response.status_code == 202
        task_id = response.json()["task_id"]
        _run_one_queued_task(profiled_client)

        get_response = profiled_client.get(f"/tasks/{task_id}")
        assert get_response.status_code == 200
        payload = get_response.json()
        latest_run = payload["latest_run"]

        assert payload["chosen_profile"] == "codex-tool-loop-executor"
        assert payload["runtime_mode"] == "tool_loop"
        assert latest_run["worker_profile"] == "codex-tool-loop-executor"
        assert latest_run["runtime_mode"] == "tool_loop"
        assert len(worker.requests) == 1
        assert worker.requests[0].worker_profile == "codex-tool-loop-executor"
        assert worker.requests[0].runtime_mode == "tool_loop"


def test_queued_task_requires_approval_before_worker_dispatch(client: TestClient) -> None:
    """requires_approval should halt execution before the worker is invoked."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"requires_approval": True, "approval_reason": "Manual safety gate"},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    get_response = client.get(f"/tasks/{task_id}")
    assert get_response.status_code == 200
    payload = get_response.json()
    assert payload["status"] == "pending"
    assert payload["latest_run"] is not None
    assert payload["latest_run"]["status"] == "failure"
    assert "approval" in payload["latest_run"]["summary"].lower()

    worker = client.app.state.test_worker
    assert len(worker.requests) == 0


def test_queued_task_ignores_injected_approval_constraints(client: TestClient) -> None:
    """User-supplied approval status must not bypass destructive-task approval gates."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"approval": {"status": "approved", "source": "api"}},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval-spoof",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    get_response = client.get(f"/tasks/{task_id}")
    assert get_response.status_code == 200
    payload = get_response.json()
    assert payload["status"] == "pending"
    assert payload["latest_run"] is not None
    assert payload["latest_run"]["status"] == "failure"
    assert "approval" in payload["latest_run"]["summary"].lower()

    worker = client.app.state.test_worker
    assert len(worker.requests) == 0

    with session_scope(client.app.state.task_service.session_factory) as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert isinstance(task.constraints, dict)
        approval = task.constraints.get("approval")
        assert isinstance(approval, dict)
        assert approval.get("source") == "orchestrator"


def test_task_approval_endpoint_requeues_approved_task(client: TestClient) -> None:
    """Approving a paused task should requeue it for the next worker claim."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"requires_approval": True, "approval_reason": "Manual safety gate"},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval-resume",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    paused = client.get(f"/tasks/{task_id}")
    assert paused.status_code == 200
    assert paused.json()["status"] == "pending"
    assert "approval" in paused.json()["latest_run"]["summary"].lower()

    approve_response = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "pending"

    duplicate_approve = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert duplicate_approve.status_code == 200
    assert duplicate_approve.json()["status"] == "pending"

    _run_one_queued_task(client)
    resumed = client.get(f"/tasks/{task_id}")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"

    worker = client.app.state.test_worker
    assert len(worker.requests) == 1


def test_task_approval_endpoint_rejects_task_terminally(client: TestClient) -> None:
    """Rejecting a paused task should keep it terminal with a clear decision summary."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"requires_approval": True},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval-reject",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    reject_response = client.post(f"/tasks/{task_id}/approval", json={"approved": False})
    assert reject_response.status_code == 200
    payload = reject_response.json()
    assert payload["status"] == "failed"
    assert payload["latest_run"] is not None
    assert "rejected" in payload["latest_run"]["summary"].lower()

    duplicate_reject = client.post(f"/tasks/{task_id}/approval", json={"approved": False})
    assert duplicate_reject.status_code == 200
    assert duplicate_reject.json()["status"] == "failed"

    worker = client.app.state.test_worker
    assert len(worker.requests) == 0


def test_task_approval_endpoint_rejects_conflicting_duplicate_decisions(client: TestClient) -> None:
    """Once approved/rejected, the opposite decision should return a conflict."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"requires_approval": True},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval-conflict",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    approve_response = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert approve_response.status_code == 200

    conflict_response = client.post(f"/tasks/{task_id}/approval", json={"approved": False})
    assert conflict_response.status_code == 409
    assert "already recorded" in conflict_response.json()["detail"].lower()


def test_task_approval_endpoint_rejects_tasks_not_waiting_for_decision(client: TestClient) -> None:
    """Approval endpoint should fail for tasks that are not in a paused-approval state."""
    response = client.post("/tasks", json={"task_text": "Create a README section"})
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    not_waiting_response = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert not_waiting_response.status_code == 409
    assert "not currently awaiting" in not_waiting_response.json()["detail"].lower()


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
    assert response.json() == {
        "detail": (
            f"Authentication required: Provide {API_SHARED_SECRET_HEADER} header or session cookie."
        )
    }
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


def test_task_creation_with_cookie_auth(client: TestClient, session_factory) -> None:
    """Task creation should work with a valid session cookie and Origin header."""
    auth_config = ApiAuthConfig(
        shared_secret="test-secret", allowed_origins=["http://localhost:3000"]
    )
    worker = client.app.state.test_worker
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=auth_config,
    )

    with TestClient(app) as test_client:
        token = create_dashboard_token(auth_config.shared_secret)
        test_client.cookies.set(DASHBOARD_COOKIE_NAME, token)

        response = test_client.post(
            "/tasks", json={"task_text": "test task"}, headers={"Origin": "http://localhost:3000"}
        )
        assert response.status_code == 202


def test_task_creation_csrf_rejection(client: TestClient, session_factory) -> None:
    """Cookie-based task creation should fail without a trusted Origin."""
    auth_config = ApiAuthConfig(
        shared_secret="test-secret", allowed_origins=["http://localhost:3000"]
    )
    worker = client.app.state.test_worker
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=auth_config,
    )

    with TestClient(app) as test_client:
        token = create_dashboard_token(auth_config.shared_secret)
        test_client.cookies.set(DASHBOARD_COOKIE_NAME, token)

        # Missing Origin
        response = test_client.post("/tasks", json={"task_text": "test task"})
        assert response.status_code == 403
        assert "CSRF protection" in response.json()["detail"]

        # Bad Origin
        response = test_client.post(
            "/tasks", json={"task_text": "test task"}, headers={"Origin": "http://malicious.com"}
        )
        assert response.status_code == 403


def test_auth_precedence_invalid_header(client: TestClient, session_factory) -> None:
    """If an invalid header is provided, fail even if a valid cookie is present."""
    auth_config = ApiAuthConfig(
        shared_secret="test-secret", allowed_origins=["http://localhost:3000"]
    )
    worker = client.app.state.test_worker
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=auth_config,
    )

    with TestClient(app) as test_client:
        token = create_dashboard_token(auth_config.shared_secret)
        test_client.cookies.set(DASHBOARD_COOKIE_NAME, token)

        response = test_client.post(
            "/tasks",
            json={"task_text": "test task"},
            headers={API_SHARED_SECRET_HEADER: "wrong-secret", "Origin": "http://localhost:3000"},
        )
        assert response.status_code == 403
        assert "Invalid API authentication secret" in response.json()["detail"]


def test_csrf_normalization_edge_cases(client: TestClient, session_factory) -> None:
    """CSRF protection should handle trailing slashes and default ports."""
    auth_config = ApiAuthConfig(
        shared_secret="test-secret", allowed_origins=["http://localhost:3000"]
    )
    worker = client.app.state.test_worker
    app = create_app(
        task_service=TaskExecutionService(session_factory=session_factory, worker=worker),
        auth_config=auth_config,
    )

    with TestClient(app) as test_client:
        token = create_dashboard_token(auth_config.shared_secret)
        test_client.cookies.set(DASHBOARD_COOKIE_NAME, token)

        # Trailing slash in Origin
        response = test_client.post(
            "/tasks", json={"task_text": "test task"}, headers={"Origin": "http://localhost:3000/"}
        )
        assert response.status_code == 202

        # Referer fallback with path
        response = test_client.post(
            "/tasks",
            json={"task_text": "test task"},
            headers={"Referer": "http://localhost:3000/some/path?query=1"},
        )
        assert response.status_code == 202
