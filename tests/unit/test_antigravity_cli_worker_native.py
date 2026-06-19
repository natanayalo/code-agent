"""Unit tests for Antigravity-backed native worker execution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from db.enums import WorkerRuntimeMode
from sandbox import WorkspaceCleanupPolicy, WorkspaceHandle
from workers import AntigravityCliRuntimeAdapter, GeminiCliWorker, WorkerRequest
from workers.base import WorkerResult
from workers.native_agent_runner import NativeAgentRunResult


class _FakeWorkspaceManager:
    def __init__(self, workspace: WorkspaceHandle) -> None:
        self.workspace = workspace
        self.cleanup_requests: list[tuple[WorkspaceHandle, bool]] = []

    def create_workspace(self, request: object) -> WorkspaceHandle:
        return self.workspace

    def cleanup_workspace(self, workspace: WorkspaceHandle, *, succeeded: bool) -> bool:
        self.cleanup_requests.append((workspace, succeeded))
        return False


class _FakeContainerManager:
    def stop(self, container: object) -> None:
        raise AssertionError("native Antigravity run should not start a tool-loop container")


def _make_workspace(tmp_path: Path) -> WorkspaceHandle:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(parents=True)
    return WorkspaceHandle(
        workspace_id="ws-antigravity-test",
        task_id="antigravity-test-task",
        workspace_path=tmp_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )


def _make_worker(
    tmp_path: Path,
    *,
    tool_permission: str = "proceed-in-sandbox",
    native_sandbox_enabled: bool = True,
) -> tuple[GeminiCliWorker, WorkspaceHandle]:
    workspace = _make_workspace(tmp_path)
    worker = GeminiCliWorker(
        runtime_adapter=AntigravityCliRuntimeAdapter(
            executable="/opt/bin/agy",
            model="gemini-3-pro",
            tool_permission=tool_permission,
            artifact_review_policy="auto",
        ),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(),  # type: ignore[arg-type]
        native_sandbox_enabled=native_sandbox_enabled,
    )
    return worker, workspace


def test_antigravity_worker_builds_prompt_argv_command_and_settings(tmp_path: Path) -> None:
    worker, workspace = _make_worker(tmp_path)
    native_result = NativeAgentRunResult(
        status="success",
        summary="done",
        command="/opt/bin/agy --print [REDACTED]",
        exit_code=0,
        duration_seconds=0.3,
        timed_out=False,
        final_message="Antigravity run complete.",
        files_changed=["note.txt"],
        stdout='{"response":"Antigravity run complete."}',
    )

    with patch(
        "workers.gemini_cli_worker_native.run_native_agent",
        return_value=native_result,
    ) as run_native:
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-antigravity",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Apply a small Antigravity change",
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                )
            )
        )

    native_request = run_native.call_args.args[0]
    command = native_request.command
    assert result.status == "success"
    assert result.summary == "Antigravity run complete."
    assert command[:3] == ["/opt/bin/agy", "--print", native_request.prompt]
    assert "--model" in command
    assert command[command.index("--model") + 1] == "gemini-3-pro"
    assert "--print-timeout" in command
    assert "--log-file" in command
    assert native_request.stdin_prompt is False
    assert native_request.command_redactions == [native_request.prompt]
    assert (
        native_request.events_path
        == workspace.workspace_path / ".code-agent" / "antigravity-native.log"
    )

    settings_path = (
        workspace.workspace_path / ".agent_home" / ".gemini" / "antigravity-cli" / "settings.json"
    )
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings == {
        "artifactReviewPolicy": "auto",
        "enableTerminalSandbox": True,
        "toolPermission": "proceed-in-sandbox",
    }
    assert result.budget_usage is not None
    assert result.budget_usage["native_agent"]["provider"] == "antigravity"
    assert result.budget_usage["native_agent"]["tool_permission"] == "proceed-in-sandbox"


def test_antigravity_worker_read_only_uses_strict_tool_permission(tmp_path: Path) -> None:
    worker, workspace = _make_worker(tmp_path, tool_permission="always-proceed")

    with patch(
        "workers.gemini_cli_worker_native.run_native_agent",
        return_value=NativeAgentRunResult(
            status="success",
            summary="ok",
            command="agy --print [REDACTED]",
            exit_code=0,
            duration_seconds=0.1,
            timed_out=False,
            final_message="ok",
        ),
    ):
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    repo_url="https://example.com/repo.git",
                    task_text="Inspect only",
                    constraints={"read_only": True},
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                )
            )
        )

    assert result.status == "success"
    settings_path = (
        workspace.workspace_path / ".agent_home" / ".gemini" / "antigravity-cli" / "settings.json"
    )
    assert json.loads(settings_path.read_text(encoding="utf-8"))["toolPermission"] == "strict"


def test_antigravity_worker_maps_auth_failure_to_provider_auth(tmp_path: Path) -> None:
    worker, _ = _make_worker(tmp_path)
    result = worker._build_worker_result_from_native_run(
        _make_workspace(tmp_path / "manual"),
        NativeAgentRunResult(
            status="failure",
            summary="Native agent command exited with code 2.",
            command="agy --print [REDACTED]",
            exit_code=2,
            duration_seconds=0.1,
            timed_out=False,
            stderr="authentication failed: keyring is locked",
        ),
        WorkerRuntimeMode.NATIVE_AGENT,
        None,
        {"provider": "antigravity"},
    )

    assert isinstance(result, WorkerResult)
    assert result.failure_kind == "provider_auth"


def test_antigravity_worker_maps_permission_denial_to_permission_denied(tmp_path: Path) -> None:
    worker, _ = _make_worker(tmp_path)
    result = worker._build_worker_result_from_native_run(
        _make_workspace(tmp_path / "manual"),
        NativeAgentRunResult(
            status="failure",
            summary="Native agent command exited with code 3.",
            command="agy --print [REDACTED]",
            exit_code=3,
            duration_seconds=0.1,
            timed_out=False,
            stderr="permission denied by tool permission policy",
        ),
        WorkerRuntimeMode.NATIVE_AGENT,
        None,
        {"provider": "antigravity"},
    )

    assert result.failure_kind == "permission_denied"
    assert result.next_action_hint == "request_higher_permission"
