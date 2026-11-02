"""Operator-flow smoke for read-only shell requests, without Docker authority."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from orchestrator.execution import TaskExecutionService
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle
from tests.integration.task_endpoints_support import _run_one_temporal_task
from tests.integration.test_telegram_endpoints import RecordingProgressNotifier
from workers import WorkerResult
from workers.shell_worker import ShellWorker


def test_readonly_operator_task_reaches_shell_container_boundary(
    session_factory, tmp_path, monkeypatch
):
    monkeypatch.setenv("CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED", "0")
    handle = WorkspaceHandle(
        workspace_id="workspace-m2-shell",
        task_id="m2-shell",
        workspace_path=tmp_path,
        repo_path=tmp_path,
        repo_url="https://example.com/synthetic.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )
    workspaces, containers = MagicMock(), MagicMock()
    workspaces.create_workspace.return_value = handle
    workspaces.get_workspace.return_value = handle
    worker = ShellWorker(workspace_manager=workspaces, container_manager=containers)
    monkeypatch.setattr(
        worker, "available_workers", lambda: {"codex": worker, "shell": worker}, raising=False
    )
    notifier = RecordingProgressNotifier()
    service = TaskExecutionService(
        session_factory=session_factory, worker=worker, progress_notifier=notifier
    )
    app = create_app(task_service=service, auth_config=ApiAuthConfig(shared_secret="a" * 32))
    with (
        TestClient(app) as client,
        patch(
            "workers.shell_worker._run_shell_script",
            return_value=WorkerResult(
                status="success",
                summary="Synthetic read-only smoke passed",
                commands_run=[
                    {"command": "printf inspection", "exit_code": 0, "duration_seconds": 0.1}
                ],
                test_results=[{"name": "synthetic-inspection", "status": "passed"}],
            ),
        ),
    ):
        app.state.system_config = replace(
            app.state.system_config, allowed_repos={"synthetic": handle.repo_url}
        )
        response = client.post(
            "/webhook",
            headers={"X-Webhook-Token": "a" * 32},
            json={
                "task_text": "Inspect the repository",
                "repo_key": "synthetic",
                "constraints": {"read_only": True},
            },
        )
        assert response.status_code == 202
        _run_one_temporal_task(client)
        task = service.get_task(response.json()["task_id"])
        assert task.status == "completed", task.latest_run.summary if task.latest_run else "no run"
    assert containers.start.call_count >= 1
    assert all(call.args[0].read_only_workspace for call in containers.start.call_args_list)
    assert containers.stop.call_count == containers.start.call_count
    phases = [event.phase for event in notifier.events]
    assert phases[0] == "started" and phases[-1] == "completed" and "running" in phases
