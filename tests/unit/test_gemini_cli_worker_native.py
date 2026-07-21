"""Unit tests for the Gemini CLI worker."""

from __future__ import annotations

import asyncio
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
from workers import GeminiCliWorker, WorkerRequest, WorkerResult
from workers.base import ArtifactReference
from workers.cli_runtime import CliRuntimeMessage, CliRuntimeStep
from workers.gemini_cli_worker import _prepare_workspace_gemini_home
from workers.gemini_cli_worker_native import GeminiCliWorkerNativeMixin
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


class _BareNativeMixin(GeminiCliWorkerNativeMixin):
    def __init__(self) -> None:
        self.runtime_adapter = _ScriptedAdapter([])


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


def _make_workspace(tmp_path: Path) -> WorkspaceHandle:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    return WorkspaceHandle(
        workspace_id="ws-gemini-test",
        task_id="gemini-cli-test-task",
        workspace_path=tmp_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )


def _make_container(workspace: WorkspaceHandle) -> DockerSandboxContainer:
    return DockerSandboxContainer(
        working_dir="/workspace",
        container_name="test-gemini-container",
        image="python:3.12-slim",
        workspace=workspace,
    )


def _git_status_command(container_workdir: str = "/workspace/repo") -> str:
    return f"git -C {container_workdir} status --porcelain=v1 -z --untracked-files=all"


def test_gemini_cli_worker_runs_native_agent_mode_when_requested(tmp_path: Path) -> None:
    """Gemini native mode should invoke one-shot runner and skip tool-loop container setup."""
    adapter = _ScriptedAdapter([])
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=workspace_manager,
        container_manager=container_manager,
    )
    native_result = NativeAgentRunResult(
        status="success",
        summary="Native command completed.",
        command="gemini --prompt 'task' --output-format json",
        exit_code=0,
        duration_seconds=1.4,
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
        stdout='{"response":"Native run complete.","stats":{}}',
        stderr="",
        json_payload={
            "suggested_worker": "antigravity",
            "suggested_profile": "antigravity-native-executor-read-only",
        },
    )

    with patch(
        "workers.gemini_cli_worker_native.run_native_agent",
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
                    response_format="json",
                )
            )
        )

    assert result.status == "success"
    assert result.summary == "Native run complete."
    assert result.json_payload == {
        "suggested_worker": "antigravity",
        "suggested_profile": "antigravity-native-executor-read-only",
    }
    assert result.files_changed == ["note.txt"]
    assert result.diff_text == "diff --git a/note.txt b/note.txt"
    assert result.budget_usage is not None
    assert result.budget_usage["runtime_mode"] == "native_agent"
    assert result.commands_run[0].command.startswith("gemini")
    assert container_manager.start_requests == []
    assert container_manager.stop_requests == []
    assert workspace_manager.cleanup_requests == [(workspace, True)]
    command = run_native.call_args.args[0].command
    assert "--output-format" in command
    assert command[command.index("--output-format") + 1] == "json"
    assert "--approval-mode" in command
    assert command[command.index("--approval-mode") + 1] == "yolo"
    assert "--prompt" not in command
    assert "--sandbox" in command
    native_request = run_native.call_args.args[0]
    assert "## Native Execution Task" in native_request.prompt
    assert "Apply a small native worker change" in native_request.prompt
    assert result.artifacts[0].name == "workspace"


def test_gemini_cli_worker_native_mode_honors_read_only_constraint(tmp_path: Path) -> None:
    """Read-only constraints should map Gemini native runs to plan approval mode."""
    adapter = _ScriptedAdapter([])
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=workspace_manager,
        container_manager=container_manager,
    )
    native_result = NativeAgentRunResult(
        status="success",
        summary="Native command completed.",
        command="gemini --output-format json --approval-mode plan",
        exit_code=0,
        duration_seconds=0.4,
        timed_out=False,
        final_message='{"status":"passed","summary":"verification passed"}',
        diff_text="",
        files_changed=[],
        artifacts=[],
        stdout='{"response":"ok"}',
        stderr="",
    )

    with patch(
        "workers.gemini_cli_worker_native.run_native_agent",
        return_value=native_result,
    ) as run_native:
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-native-read-only",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Verify read-only behavior",
                    constraints={"read_only": True},
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                )
            )
        )

    assert result.status == "success"
    command = run_native.call_args.args[0].command
    assert "--approval-mode" in command
    assert command[command.index("--approval-mode") + 1] == "plan"


def test_gemini_native_prompt_includes_schema_and_planner_role(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path)
    worker = GeminiCliWorker(
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(_make_container(workspace)),
        runtime_adapter=_ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")]),
        tool_registry=DEFAULT_TOOL_REGISTRY,
    )
    prompt = worker._build_native_prompt(
        system_prompt="sys",
        request=WorkerRequest(
            task_text="Plan this task",
            repo_url="https://example.com/repo.git",
            response_schema={"type": "object"},
        ),
        runtime_mode=WorkerRuntimeMode.PLANNER_ONLY,
    )
    assert "Specialist Role: Planner" in prompt
    assert "Return exactly one JSON object that strictly matches this JSON schema" in prompt


def test_gemini_native_mixin_defaults_missing_native_sandbox_flag(tmp_path: Path) -> None:
    worker = _BareNativeMixin()
    command = worker._build_native_command(
        request=WorkerRequest(repo_url="https://example.com/repo.git", task_text="run"),
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
    )
    native_env = worker._native_run_env()
    result = worker._build_worker_result_from_native_run(
        _make_workspace(tmp_path),
        NativeAgentRunResult(
            status="success",
            summary="done",
            command="gemini --output-format text",
            exit_code=0,
            duration_seconds=0.1,
            timed_out=False,
            final_message="done",
        ),
        WorkerRuntimeMode.NATIVE_AGENT,
        None,
    )

    assert "--sandbox" in command
    assert native_env is not None
    assert native_env["GEMINI_SANDBOX"] == "true"
    assert result.budget_usage is not None
    assert result.budget_usage["native_agent"]["sandbox_enabled"] is True


def test_prepare_workspace_gemini_home_creates_workspace_mapping(tmp_path: Path) -> None:
    workspace_path = tmp_path / "workspace"
    source_gemini_home = tmp_path / "root-gemini"
    workspace_path.mkdir(parents=True)
    source_gemini_home.mkdir(parents=True)
    (source_gemini_home / "settings.json").write_text('{"auth":"ok"}', encoding="utf-8")

    _prepare_workspace_gemini_home(
        workspace_path=workspace_path,
        source_gemini_home=source_gemini_home,
    )

    target = workspace_path / ".agent_home" / ".gemini"
    assert target.exists()
    assert (target / "settings.json").read_text(encoding="utf-8") == '{"auth":"ok"}'


def test_prepare_workspace_gemini_home_uses_gemini_home_env(tmp_path: Path, monkeypatch) -> None:
    workspace_path = tmp_path / "workspace"
    source_gemini_home = tmp_path / "custom-gemini-home"
    workspace_path.mkdir(parents=True)
    source_gemini_home.mkdir(parents=True)
    (source_gemini_home / "settings.json").write_text('{"auth":"env"}', encoding="utf-8")
    monkeypatch.setenv("GEMINI_HOME", str(source_gemini_home))

    _prepare_workspace_gemini_home(workspace_path=workspace_path)

    target = workspace_path / ".agent_home" / ".gemini"
    assert target.exists()
    assert (target / "settings.json").read_text(encoding="utf-8") == '{"auth":"env"}'


def test_prepare_workspace_gemini_home_repairs_stale_target_missing_settings(
    tmp_path: Path,
) -> None:
    workspace_path = tmp_path / "workspace"
    source_gemini_home = tmp_path / "root-gemini"
    workspace_path.mkdir(parents=True)
    source_gemini_home.mkdir(parents=True)
    (source_gemini_home / "settings.json").write_text('{"auth":"ok"}', encoding="utf-8")

    stale_target = workspace_path / ".agent_home" / ".gemini"
    stale_target.mkdir(parents=True)
    (stale_target / "history").mkdir(parents=True)

    _prepare_workspace_gemini_home(
        workspace_path=workspace_path,
        source_gemini_home=source_gemini_home,
    )

    target = workspace_path / ".agent_home" / ".gemini"
    assert target.exists()
    assert (target / "settings.json").read_text(encoding="utf-8") == '{"auth":"ok"}'


def test_gemini_cli_worker_run_prepares_workspace_home_before_native_dispatch(
    tmp_path: Path,
) -> None:
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: _FakeSession({}),
    )

    with (
        patch("workers.gemini_cli_worker._prepare_workspace_gemini_home") as prepare_home,
        patch.object(
            worker,
            "_execute_native_runtime",
            return_value=WorkerResult(status="success", summary="ok"),
        ) as execute_native,
    ):
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    task_text="run",
                    repo_url="https://example.com/repo.git",
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                )
            )
        )

    assert result.status == "success"
    prepare_home.assert_called_once_with(workspace_path=workspace.workspace_path)
    execute_native.assert_called_once()


def test_gemini_cli_worker_run_prepares_workspace_home_for_blank_scratch_namespace(
    tmp_path: Path,
) -> None:
    """A blank namespace is not an isolated fan-out namespace."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: _FakeSession({}),
    )

    with (
        patch("workers.gemini_cli_worker._prepare_workspace_gemini_home") as prepare_home,
        patch.object(
            worker,
            "_execute_native_runtime",
            return_value=WorkerResult(status="success", summary="ok"),
        ) as execute_native,
    ):
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    task_text="run",
                    repo_url="https://example.com/repo.git",
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                    scratch_namespace="",
                )
            )
        )

    assert result.status == "success"
    prepare_home.assert_called_once_with(workspace_path=workspace.workspace_path)
    execute_native.assert_called_once()


def test_gemini_cli_worker_run_skips_workspace_home_for_scratch_node(
    tmp_path: Path,
) -> None:
    """Fan-out nodes keep provider state in their external scratch namespace."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: _FakeSession({}),
    )

    with (
        patch("workers.gemini_cli_worker._prepare_workspace_gemini_home") as prepare_home,
        patch.object(
            worker,
            "_execute_native_runtime",
            return_value=WorkerResult(status="success", summary="ok"),
        ) as execute_native,
    ):
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    task_text="inspect",
                    repo_url="https://example.com/repo.git",
                    runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
                    scratch_namespace="node-activity:v1:plan:inspect:1",
                )
            )
        )

    assert result.status == "success"
    prepare_home.assert_not_called()
    execute_native.assert_called_once()
