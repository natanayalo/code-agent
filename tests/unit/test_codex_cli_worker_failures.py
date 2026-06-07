"""Unit tests for the Codex CLI worker."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Literal

import pytest

from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerRequest,
    DockerShellCommandResult,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
)
from workers import CodexCliWorker, ReviewResult, WorkerRequest
from workers.cli_runtime import CliRuntimeMessage, CliRuntimeStep


class _FakeWorkspaceManager:
    def __init__(self, workspace: WorkspaceHandle) -> None:
        self.workspace = workspace
        self.requests: list[object] = []
        self.cleanup_requests: list[tuple[WorkspaceHandle, bool]] = []

    def create_workspace(self, request: object) -> WorkspaceHandle:
        self.requests.append(request)
        return self.workspace

    def cleanup_workspace(self, workspace: WorkspaceHandle, *, succeeded: bool) -> bool:
        self.cleanup_requests.append((workspace, succeeded))
        return False


class _FakeContainerManager:
    def __init__(self, container: DockerSandboxContainer) -> None:
        self.container = container
        self.start_requests: list[DockerSandboxContainerRequest] = []
        self.stop_requests: list[DockerSandboxContainer] = []

    def start(self, request: DockerSandboxContainerRequest) -> DockerSandboxContainer:
        self.start_requests.append(request)
        return self.container

    def stop(self, container: DockerSandboxContainer) -> None:
        self.stop_requests.append(container)


class _ScriptedAdapter:
    def __init__(self, steps: list[CliRuntimeStep]) -> None:
        self._steps = list(steps)
        self.calls: list[list[CliRuntimeMessage]] = []
        self.prompt_overrides: list[str | None] = []

    def next_step(
        self,
        messages: list[CliRuntimeMessage],
        *,
        system_prompt: str | None = None,
        prompt_override: str | None = None,
        working_directory: Path | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        response_format: Literal["text", "json"] = "text",
        response_schema: dict[str, Any] | None = None,
    ) -> CliRuntimeStep:
        self.calls.append(list(messages))
        self.prompt_overrides.append(prompt_override)
        if not self._steps:
            raise AssertionError("Adapter received more turns than expected.")
        return self._steps.pop(0)


class _FakeSession:
    def __init__(self, responses: dict[str, DockerShellCommandResult]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        self.calls.append((command, timeout_seconds))
        return self.responses[command]

    def close(self) -> None:
        self.closed = True


def _workspace_handle(tmp_path: Path) -> WorkspaceHandle:
    workspace_path = tmp_path / "workspace-task-47"
    repo_path = workspace_path
    repo_path.mkdir(parents=True)
    (repo_path / "AGENTS.md").write_text("Prefer small diffs.\n", encoding="utf-8")
    (repo_path / "README.md").write_text("# Demo repo\n", encoding="utf-8")
    return WorkspaceHandle(
        workspace_id="workspace-task-47",
        task_id="task-47",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )


def _command_result(command: str, *, output: str, exit_code: int = 0) -> DockerShellCommandResult:
    return DockerShellCommandResult(
        command=command,
        output=output,
        exit_code=exit_code,
        duration_seconds=0.1,
    )


def _git_status_command(container_workdir: str = "/workspace") -> str:
    return f"git -C {container_workdir} status --porcelain=v1 -z --untracked-files=all"


def _review_result_json(
    *,
    outcome: str,
    summary: str,
) -> str:
    if outcome == "no_findings":
        payload = ReviewResult(
            reviewer_kind="worker_self_review",
            summary=summary,
            confidence=0.92,
            outcome="no_findings",
            findings=[],
        )
    else:
        payload = ReviewResult.model_validate(
            {
                "reviewer_kind": "worker_self_review",
                "summary": summary,
                "confidence": 0.88,
                "outcome": "findings",
                "findings": [
                    {
                        "severity": "medium",
                        "category": "missing-test",
                        "confidence": 0.8,
                        "file_path": "note.txt",
                        "line_start": 1,
                        "line_end": 1,
                        "title": "Missing follow-up assertion",
                        "why_it_matters": "Behavior can regress without a focused test.",
                        "evidence": "No test command appears in the run transcript.",
                        "suggested_fix": "Add and execute a focused unit test.",
                    }
                ],
            }
        )
    return json.dumps(payload.model_dump(mode="json"))


def test_codex_cli_worker_requests_higher_permission_for_blocked_commands(tmp_path: Path) -> None:
    """Permission-required runtime failures should map to a clear worker follow-up hint."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter(
        [CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="rm -rf build")]
    )
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output="",
            )
        }
    )
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda started_container, **_: session,
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-49-permission",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Attempt a blocked shell action",
                constraints={"granted_permission": "workspace_write"},
            )
        )
    )

    assert result.status == "failure"
    assert result.next_action_hint == "request_higher_permission"
    assert "dangerous_shell" in (result.summary or "")


@pytest.mark.anyio
async def test_codex_cli_worker_returns_partial_result_on_cancellation(tmp_path: Path) -> None:
    """The CLI worker should catch cancellation and return the partial runtime state."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-cancel",
        image="python:3.12-slim",
    )
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)

    class _HangingAdapter:
        def __init__(self):
            self.called = asyncio.Event()

        def next_step(self, messages, **kwargs):
            # Simulate a very long thought/command
            time.sleep(1.2)
            return CliRuntimeStep(kind="final", final_output="Unreachable")

    adapter = _HangingAdapter()
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output="",
            )
        }
    )
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=workspace_manager,
        container_manager=container_manager,
        session_factory=lambda started_container, **_: session,
    )

    request = WorkerRequest(
        session_id="session-cancel",
        repo_url="https://example.com/repo.git",
        branch="main",
        task_text="This will be cancelled",
    )

    # Start the worker task
    worker_task = asyncio.create_task(worker.run(request))

    # Wait a bit for it to start
    await asyncio.sleep(0.5)

    # Cancel it
    worker_task.cancel()

    # Wait for the worker to settle and yield the partial result
    result = await worker_task

    assert result.status == "error"
    assert result.next_action_hint == "inspect_workspace_artifacts"
    assert "loop was cancelled" in (result.summary or "")
    assert workspace_manager.cleanup_requests == [(workspace, False)]
    assert container_manager.stop_requests == [container]
    assert session.closed is True


def test_codex_cli_worker_stops_container_when_setup_fails_after_start(tmp_path: Path) -> None:
    """Setup failures after container start should still clean up the started container."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-setup-fail",
        image="python:3.12-slim",
    )
    container_manager = _FakeContainerManager(container)

    def _failing_session_factory(started_container, **_):
        raise OSError("session init failed")

    worker = CodexCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=container_manager,
        session_factory=_failing_session_factory,
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-setup-fail",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Trigger setup failure",
            )
        )
    )

    assert result.status == "error"
    assert "session init failed" in (result.summary or "")
    assert container_manager.stop_requests == [container]
