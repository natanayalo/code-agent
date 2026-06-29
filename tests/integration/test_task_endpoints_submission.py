"""Integration tests for task submission and polling endpoints."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.enums import ArtifactType, TaskStatus, WorkerRunStatus, WorkerRuntimeMode
from orchestrator.execution import TaskExecutionService
from repositories import (
    ArtifactRepository,
    TaskRepository,
    WorkerRunRepository,
    session_scope,
)
from tests.integration.task_endpoints_support import (
    DEFAULT_SHARED_SECRET,
    StaticWorker,
    _run_one_queued_task,
)
from workers import WorkerProfile, WorkerResult


def _assert_task_response_payload(payload: dict) -> None:
    assert payload["status"] == "pending"
    assert payload["chosen_worker"] is None
    assert payload["route_reason"] is None
    assert payload["task_spec"]["goal"] == "Create a note and report the result"
    assert payload["task_spec"]["task_type"] == "feature"
    assert payload["task_spec"]["risk_level"] == "low"
    assert payload["latest_run"] is None


def _assert_completed_task_response(
    get_response, task_id: str, payload: dict, latest_run: dict, worker
) -> None:
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

    assert len(worker.requests) == 1
    assert worker.requests[0].session_id == payload["session_id"]
    assert worker.requests[0].repo_url == "https://github.com/natanayalo/code-agent"
    assert worker.requests[0].branch == "master"
    assert worker.requests[0].task_spec is not None
    assert worker.requests[0].task_spec["goal"] == "Create a note and report the result"


def _assert_task_persistence_records(session_factory, task_id: str, payload: dict) -> None:
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


def test_submit_task_persists_execution_path_and_allows_polling(
    client: TestClient, session_factory
) -> None:
    """Submitting a task should return a pollable snapshot and persist the eventual result."""
    client.app.state.system_config.allowed_repos["code-agent"] = (
        "https://github.com/natanayalo/code-agent"
    )
    response = client.post(
        "/tasks",
        json={
            "task_text": "Create a note and report the result",
            "repo_key": "code-agent",
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

    _assert_task_response_payload(payload)

    _run_one_queued_task(client)

    get_response = client.get(f"/tasks/{task_id}")
    latest_run = get_response.json()["latest_run"]

    worker = client.app.state.test_worker
    _assert_completed_task_response(get_response, task_id, payload, latest_run, worker)
    _assert_task_persistence_records(session_factory, task_id, payload)


def _assert_profiled_task_metadata(payload: dict, latest_run: dict, worker: StaticWorker) -> None:
    assert payload["chosen_worker"] == "codex"
    assert payload["chosen_profile"] == "codex-native-executor"
    assert payload["runtime_mode"] == "native_agent"
    assert latest_run["worker_type"] == "codex"
    assert latest_run["worker_profile"] == "codex-native-executor"
    assert latest_run["runtime_mode"] == "native_agent"
    assert len(worker.requests) == 1
    assert worker.requests[0].worker_profile == "codex-native-executor"
    assert worker.requests[0].runtime_mode == "native_agent"


def _assert_profiled_persistence_records(session_factory, task_id: str) -> None:
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
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )
    app.state.test_worker = worker

    with TestClient(app) as profiled_client:
        profiled_client.app.state.system_config.allowed_repos["code-agent"] = (
            "https://github.com/natanayalo/code-agent"
        )
        profiled_client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET
        response = profiled_client.post(
            "/tasks",
            json={
                "task_text": "Apply a profiled codex native task",
                "repo_key": "code-agent",
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

        _assert_profiled_task_metadata(payload, latest_run, worker)
        _assert_profiled_persistence_records(session_factory, task_id)


def _assert_legacy_override_metadata(payload: dict, latest_run: dict, worker: StaticWorker) -> None:
    assert payload["chosen_profile"] == "codex-tool-loop-executor"
    assert payload["runtime_mode"] == "tool_loop"
    assert latest_run["worker_profile"] == "codex-tool-loop-executor"
    assert latest_run["runtime_mode"] == "tool_loop"
    assert len(worker.requests) == 1
    assert worker.requests[0].worker_profile == "codex-tool-loop-executor"
    assert worker.requests[0].runtime_mode == "tool_loop"


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
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )
    app.state.test_worker = worker

    with TestClient(app) as profiled_client:
        profiled_client.app.state.system_config.allowed_repos["code-agent"] = (
            "https://github.com/natanayalo/code-agent"
        )
        profiled_client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET
        response = profiled_client.post(
            "/tasks",
            json={
                "task_text": "Run with explicit codex tool-loop legacy profile",
                "repo_key": "code-agent",
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

        _assert_legacy_override_metadata(payload, latest_run, worker)


def test_task_endpoints_reject_unknown_worker_profile_override(session_factory) -> None:
    """Task submissions should fail fast when worker_profile_override is not configured."""
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
            },
        ),
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )

    with TestClient(app) as profiled_client:
        profiled_client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET
        response = profiled_client.post(
            "/tasks",
            json={
                "task_text": "Run with unknown profile",
                "worker_profile_override": "codex-tool-loop-executor",
            },
        )

        assert response.status_code == 422
        assert "unknown profile" in response.json()["detail"].lower()
        assert worker.requests == []


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


def test_submit_task_rejects_invalid_scout_budget(client: TestClient) -> None:
    """Submissions with invalid budget configurations for scout mode should return 422."""
    client.app.state.system_config.allowed_repos["code-agent"] = (
        "https://github.com/natanayalo/code-agent"
    )
    response = client.post(
        "/tasks",
        headers={},
        json={
            "task_text": "Run a scout task",
            "repo_key": "code-agent",
            "constraints": {"task_type": "scout"},
            "budget": {"max_iterations": "inf"},
        },
    )

    assert response.status_code == 422
    assert "Invalid budget configuration for max_iterations: inf" in response.json()["detail"]


def test_submit_task_rejects_extra_fields(client: TestClient) -> None:
    """Submissions with extra fields (like raw repo_url) should return 422."""
    response = client.post(
        "/tasks",
        headers={},
        json={
            "task_text": "Run a scout task",
            "repo_url": "https://github.com/natanayalo/code-agent",
        },
    )

    assert response.status_code == 422
