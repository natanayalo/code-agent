"""Unit tests for the Codex CLI worker."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerRequest,
    DockerShellCommandResult,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
)
from tools import DEFAULT_TOOL_REGISTRY, ToolRegistry
from workers import CodexCliWorker, WorkerRequest
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

    def next_step(self, messages: list[CliRuntimeMessage]) -> CliRuntimeStep:
        self.calls.append(list(messages))
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
    repo_path = workspace_path / "repo"
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


def test_codex_cli_worker_requires_repo_url(tmp_path: Path) -> None:
    """The CLI worker should fail fast when it cannot provision a workspace."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    worker = CodexCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
    )

    result = asyncio.run(worker.run(WorkerRequest(task_text="Inspect the repo")))

    assert result.status == "error"
    assert result.summary == (
        "CodexCliWorker requires a non-empty repo_url to provision a sandbox workspace."
    )
    assert result.next_action_hint == "provide_repo_url"


def test_codex_cli_worker_runs_the_shared_runtime_and_retains_the_workspace(
    tmp_path: Path,
) -> None:
    """The CLI worker should drive the shared runtime through workspace and container setup."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_bash",
                tool_input="printf 'done\\n' > note.txt",
            ),
            CliRuntimeStep(
                kind="final", final_output="Created note.txt and summarized the change."
            ),
        ]
    )
    session = _FakeSession(
        {
            "printf 'done\\n' > note.txt": _command_result(
                "printf 'done\\n' > note.txt",
                output="",
            ),
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output="?? note.txt\0",
            ),
        }
    )
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=workspace_manager,
        container_manager=container_manager,
        session_factory=lambda started_container: session,
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-47",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Create a note and report the result",
                budget={"max_iterations": 3, "command_timeout_seconds": 5},
            )
        )
    )

    assert result.status == "success"
    assert result.summary == "Created note.txt and summarized the change."
    assert [command.command for command in result.commands_run] == ["printf 'done\\n' > note.txt"]
    assert result.files_changed == ["note.txt"]
    assert result.next_action_hint == "inspect_workspace_artifacts"
    assert result.artifacts[0].name == "workspace"
    assert result.artifacts[0].uri == str(workspace.workspace_path)
    assert workspace_manager.cleanup_requests == [(workspace, True)]
    assert container_manager.start_requests[0].workspace == workspace
    assert container_manager.stop_requests == [container]
    assert session.closed is True
    assert session.calls[-1] == ("git status --porcelain=v1 -z --untracked-files=all", 5)

    first_prompt = adapter.calls[0][0].content
    assert "## Available Tools" in first_prompt
    assert "`execute_bash`" in first_prompt
    assert "Required permission: `workspace_write`" in first_prompt
    assert "Expected artifacts: `stdout`, `stderr`, `changed_files`" in first_prompt
    assert "AGENTS.md guidance:" in first_prompt
    assert "README.md" in first_prompt


def test_codex_cli_worker_skips_changed_file_collection_when_tool_does_not_expect_it(
    tmp_path: Path,
) -> None:
    """Changed-file collection should be driven by the registered tool metadata."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    tool_registry = ToolRegistry(
        tools=(
            DEFAULT_TOOL_REGISTRY.require_tool("execute_bash").model_copy(
                update={"expected_artifacts": ()}
            ),
        )
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Nothing changed.")])
    session = _FakeSession({})
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda started_container: session,
        tool_registry=tool_registry,
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-47-artifacts",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Skip changed-file collection",
            )
        )
    )

    assert result.status == "success"
    assert result.files_changed == []
    assert session.calls == []


def test_codex_cli_worker_uses_the_full_git_status_timeout_budget(tmp_path: Path) -> None:
    """Changed-file collection should respect the configured command timeout directly."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Nothing to do.")])
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output="",
            )
        }
    )
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda started_container: session,
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-47-timeout",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Inspect timeout handling",
                budget={"command_timeout_seconds": 17},
            )
        )
    )

    assert result.status == "success"
    assert session.calls[-1] == ("git status --porcelain=v1 -z --untracked-files=all", 17)
