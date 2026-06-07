"""Unit tests for the Codex CLI worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

from db.enums import WorkerRuntimeMode
from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerRequest,
    DockerShellCommandResult,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
)
from tools import DEFAULT_TOOL_REGISTRY
from workers import CodexCliWorker, ReviewResult, WorkerRequest
from workers.base import ArtifactReference
from workers.cli_runtime import CliRuntimeMessage, CliRuntimeSettings, CliRuntimeStep
from workers.native_agent_runner import NativeAgentRunResult


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


def test_codex_cli_worker_runs_native_agent_mode_when_requested(tmp_path: Path) -> None:
    """Codex native mode should invoke one-shot runner and skip tool-loop container setup."""
    adapter = _ScriptedAdapter([])
    # Regression guard: adapter defaults must not force native mode into read-only.
    adapter.sandbox_mode = "read-only"
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-native-mode",
        image="python:3.12-slim",
    )
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=workspace_manager,
        container_manager=container_manager,
        native_event_capture_enabled=True,
    )
    native_result = NativeAgentRunResult(
        status="success",
        summary="Native command completed.",
        command="codex exec --json -",
        exit_code=0,
        duration_seconds=1.7,
        timed_out=False,
        final_message="Native run complete.",
        diff_text="diff --git a/note.txt b/note.txt",
        files_changed=["note.txt"],
        artifacts=[
            ArtifactReference(
                name="native-agent-stdout",
                uri=(tmp_path / "stdout.txt").as_uri(),
                artifact_type="log",
            )
        ],
        stdout='{"event":"turn.completed"}\n',
        stderr="",
        json_payload={"status": "passed", "summary": "structured"},
    )

    with patch(
        "workers.codex_cli_worker_native.run_native_agent",
        return_value=native_result,
    ) as run_native:
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-native-mode",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Apply a small native worker change",
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                )
            )
        )

    assert result.status == "success"
    assert result.summary == "Native run complete."
    assert result.json_payload == {"status": "passed", "summary": "structured"}
    assert result.files_changed == ["note.txt"]
    assert result.diff_text == "diff --git a/note.txt b/note.txt"
    assert result.budget_usage is not None
    assert result.budget_usage["runtime_mode"] == "native_agent"
    assert result.commands_run[0].command.startswith("codex exec")
    assert container_manager.start_requests == []
    assert container_manager.stop_requests == []
    assert workspace_manager.cleanup_requests == [(workspace, True)]
    command = run_native.call_args.args[0].command
    assert command[0:2] == ["codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "--json" in command
    assert result.artifacts[0].name == "workspace"


def test_codex_cli_worker_native_mode_honors_read_only_constraint(tmp_path: Path) -> None:
    """Native mode should force read-only sandbox when the task constraint requires it."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-native-read-only",
        image="python:3.12-slim",
    )
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    worker = CodexCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=workspace_manager,
        container_manager=container_manager,
        native_sandbox_mode="workspace-write",
    )
    native_result = NativeAgentRunResult(
        status="success",
        summary="Native command completed.",
        command="codex exec -",
        exit_code=0,
        duration_seconds=0.9,
        timed_out=False,
        final_message="Native read-only run complete.",
        artifacts=[],
        files_changed=[],
        stdout="",
        stderr="",
    )

    with patch(
        "workers.codex_cli_worker_native.run_native_agent",
        return_value=native_result,
    ) as run_native:
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-native-read-only",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Inspect only",
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                    constraints={"read_only": True},
                )
            )
        )

    command = run_native.call_args.args[0].command
    assert result.status == "success"
    assert command[command.index("--sandbox") + 1] == "read-only"


def test_codex_cli_worker_native_sandbox_logic(tmp_path: Path) -> None:
    """Codex native mode should align sandbox with container status and repo trust."""
    workspace = _workspace_handle(tmp_path)
    worker = CodexCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(None),
        trusted_repo_patterns=[r".*github\.com/trusted/.*"],
    )

    native_result = NativeAgentRunResult(
        status="success",
        summary="Success",
        command="codex exec",
        exit_code=0,
        duration_seconds=0.1,
        timed_out=False,
    )

    scenarios = [
        # (in_container, repo_url, read_only, expected_sandbox)
        (True, "https://github.com/trusted/repo", False, "danger-full-access"),
        (True, "https://github.com/untrusted/repo", False, "workspace-write"),
        (False, "https://github.com/trusted/repo", False, "workspace-write"),
        (True, "https://github.com/trusted/repo", True, "read-only"),
        (False, "https://github.com/trusted/repo", True, "read-only"),
    ]

    for in_container, repo_url, read_only, expected_sandbox in scenarios:
        with (
            patch("workers.codex_cli_worker_native.is_in_container", return_value=in_container),
            patch(
                "workers.codex_cli_worker_native.run_native_agent", return_value=native_result
            ) as run_native,
        ):
            result = asyncio.run(
                worker.run(
                    WorkerRequest(
                        task_text="test",
                        repo_url=repo_url,
                        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                        constraints={"read_only": read_only},
                    )
                )
            )

            command = run_native.call_args.args[0].command
            assert command[command.index("--sandbox") + 1] == expected_sandbox
            assert result.budget_usage["native_agent"]["sandbox_mode"] == expected_sandbox
            assert result.budget_usage["native_agent"]["in_container"] == in_container
            assert result.budget_usage["native_agent"]["repo_approved"] == (
                repo_url == "https://github.com/trusted/repo"
            )


def _setup_native_worker(tmp_path: Path) -> tuple[WorkspaceHandle, CodexCliWorker]:
    workspace = _workspace_handle(tmp_path)
    adapter = _ScriptedAdapter([])
    adapter.model = "gpt-4o"
    adapter.profile = "fast"

    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(None),
    )
    return workspace, worker


def test_codex_cli_worker_native_model_profile_overrides(tmp_path: Path) -> None:
    _, worker = _setup_native_worker(tmp_path)
    with patch("workers.codex_cli_worker_native.run_native_agent") as run_native:
        run_native.return_value = NativeAgentRunResult(
            status="success",
            summary="ok",
            command="codex",
            exit_code=0,
            duration_seconds=1,
            timed_out=False,
        )
        asyncio.run(
            worker.run(
                WorkerRequest(
                    task_text="test", repo_url="url", runtime_mode=WorkerRuntimeMode.NATIVE_AGENT
                )
            )
        )
        command = run_native.call_args.args[0].command
        assert "--model" in command
        assert "gpt-4o" in command
        assert "--profile" in command
        assert "fast" in command


def test_codex_cli_worker_native_timeout_handling(tmp_path: Path) -> None:
    _, worker = _setup_native_worker(tmp_path)
    with patch("workers.codex_cli_worker_native.run_native_agent") as run_native:
        run_native.return_value = NativeAgentRunResult(
            status="failure",
            summary="timeout",
            command="codex",
            exit_code=1,
            duration_seconds=1,
            timed_out=True,
        )
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    task_text="test", repo_url="url", runtime_mode=WorkerRuntimeMode.NATIVE_AGENT
                )
            )
        )
        assert result.status == "failure"
        assert result.failure_kind == "timeout"
        assert result.next_action_hint == "increase_budget_or_reduce_scope"


def test_codex_cli_worker_native_generic_error_handling(tmp_path: Path) -> None:
    _, worker = _setup_native_worker(tmp_path)
    with patch("workers.codex_cli_worker_native.run_native_agent") as run_native:
        run_native.return_value = NativeAgentRunResult(
            status="error",
            summary="boom",
            command="codex",
            exit_code=1,
            duration_seconds=1,
            timed_out=False,
        )
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    task_text="test", repo_url="url", runtime_mode=WorkerRuntimeMode.NATIVE_AGENT
                )
            )
        )
        assert result.status == "error"
        assert result.next_action_hint == "inspect_worker_configuration"


def test_codex_cli_worker_native_pre_execution_cancellation(tmp_path: Path) -> None:
    workspace, worker = _setup_native_worker(tmp_path)
    with patch("workers.codex_cli_worker_native.run_native_agent") as run_native:
        worker_result = worker._execute_native_runtime(
            WorkerRequest(task_text="test", repo_url="url"),
            workspace=workspace,
            runtime_settings=CliRuntimeSettings(),
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            system_prompt_override="test",
            cancel_token=lambda: True,
        )
        assert worker_result.status == "error"
        assert worker_result.failure_kind == "timeout"
        assert not run_native.called


def test_codex_native_command_includes_output_schema_path(tmp_path: Path) -> None:
    workspace = _workspace_handle(tmp_path)
    worker = CodexCliWorker(
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(
            DockerSandboxContainer(
                working_dir="/workspace",
                container_name="native-schema",
                image="python:3.12",
                workspace=workspace,
            )
        ),
        runtime_adapter=_ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")]),
        tool_registry=DEFAULT_TOOL_REGISTRY,
        native_event_capture_enabled=False,
    )
    request = WorkerRequest(
        task_text="x", repo_url="https://example.com/repo.git", response_schema={"type": "object"}
    )
    command, _ = worker._build_native_command(
        workspace=workspace,
        request=request,
        final_message_path=workspace.workspace_path / "final.json",
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        output_schema_path=workspace.workspace_path / "schema.json",
    )
    assert "--output-schema" in command


def test_codex_native_prompt_includes_response_schema_instructions(tmp_path: Path) -> None:
    workspace = _workspace_handle(tmp_path)
    worker = CodexCliWorker(
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(
            DockerSandboxContainer(
                working_dir="/workspace",
                container_name="native-prompt",
                image="python:3.12",
                workspace=workspace,
            )
        ),
        runtime_adapter=_ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")]),
        tool_registry=DEFAULT_TOOL_REGISTRY,
        native_event_capture_enabled=False,
    )
    prompt = worker._build_native_prompt(
        system_prompt="sys",
        request=WorkerRequest(
            task_text="Implement task",
            repo_url="https://example.com/repo.git",
            response_schema={"type": "object"},
        ),
    )
    assert "Return exactly one JSON object that strictly matches this JSON schema" in prompt
    assert '"type": "object"' in prompt


def test_codex_native_runtime_writes_response_schema_file(tmp_path: Path) -> None:
    workspace = _workspace_handle(tmp_path)
    worker = CodexCliWorker(
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(
            DockerSandboxContainer(
                working_dir="/workspace",
                container_name="native-write-schema",
                image="python:3.12",
                workspace=workspace,
            )
        ),
        runtime_adapter=_ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")]),
        tool_registry=DEFAULT_TOOL_REGISTRY,
        native_event_capture_enabled=False,
    )
    with patch("workers.codex_cli_worker_native.run_native_agent") as run_native:
        run_native.return_value = NativeAgentRunResult(
            status="success",
            summary='{"ok":true}',
            command="codex",
            exit_code=0,
            duration_seconds=1.0,
            timed_out=False,
        )
        result = worker._execute_native_runtime(
            WorkerRequest(
                task_text="x",
                repo_url="https://example.com/repo.git",
                response_schema={"type": "object"},
            ),
            workspace=workspace,
            runtime_settings=CliRuntimeSettings(),
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            system_prompt_override="sys",
            cancel_token=None,
        )
    schema_path = workspace.workspace_path / ".code-agent" / "native-response.schema.json"
    assert schema_path.exists()
    assert json.loads(schema_path.read_text(encoding="utf-8")) == {"type": "object"}
    assert result.status == "success"
