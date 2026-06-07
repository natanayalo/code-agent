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
from tools import DEFAULT_TOOL_REGISTRY, ToolRegistry
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


def test_codex_cli_worker_requires_repo_url(tmp_path: Path) -> None:
    """The CLI worker should fail fast when it cannot provision a workspace."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
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


def test_codex_validate_request_phase_returns_none_for_valid_repo_url(tmp_path: Path) -> None:
    """The request-validation phase should pass through when repo_url is present."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    worker = CodexCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
    )

    assert (
        worker._validate_request(
            WorkerRequest(task_text="Inspect the repo", repo_url="https://example.com/repo.git")
        )
        is None
    )


def test_codex_cli_worker_runs_the_shared_runtime_and_retains_the_workspace(
    tmp_path: Path,
) -> None:
    """The CLI worker should drive the shared runtime through workspace and container setup."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
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
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output="?? note.txt\0",
            ),
        }
    )
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=workspace_manager,
        container_manager=container_manager,
        session_factory=lambda started_container, **_: session,
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
    assert result.budget_usage is not None
    assert result.budget_usage["iterations_used"] == 2
    assert result.budget_usage["shell_commands_used"] == 1
    assert [command.command for command in result.commands_run] == ["printf 'done\\n' > note.txt"]
    assert result.files_changed == ["note.txt"]
    assert result.next_action_hint == "inspect_workspace_artifacts"
    assert result.artifacts[0].name == "workspace"
    assert result.artifacts[0].uri == workspace.workspace_path.as_uri()
    assert workspace_manager.cleanup_requests == [(workspace, True)]
    assert container_manager.start_requests[0].workspace == workspace
    assert container_manager.stop_requests == [container]
    assert session.closed is True
    assert session.calls[-1] == (_git_status_command(container.working_dir), 5)

    first_prompt = adapter.calls[0][0].content
    assert "## Available Tools" in first_prompt
    assert "`execute_bash`" in first_prompt
    assert "Required permission: `workspace_write`" in first_prompt
    assert "Expected artifacts: `stdout`, `stderr`, `changed_files`" in first_prompt
    assert "Your first action MUST be to read `AGENTS.md`" in first_prompt
    assert "README.md" in first_prompt


def test_codex_cli_worker_skips_changed_file_collection_when_tool_does_not_expect_it(
    tmp_path: Path,
) -> None:
    """Changed-file collection should be driven by the registered tool metadata."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
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
        session_factory=lambda started_container, **_: session,
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
    """Changed-file collection should use the runtime timeout, not the bash tool timeout."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Nothing to do.")])
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output="",
            )
        }
    )
    tool_registry = ToolRegistry(
        tools=(
            DEFAULT_TOOL_REGISTRY.require_tool("execute_bash").model_copy(
                update={"timeout_seconds": 3}
            ),
        )
    )
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda started_container, **_: session,
        tool_registry=tool_registry,
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
    assert session.calls[-1] == (_git_status_command(container.working_dir), 17)


def test_codex_cli_worker_runs_post_run_lint_and_appends_command_artifacts(
    tmp_path: Path,
) -> None:
    """Post-run lint/format should run after worker completion and report metadata."""
    workspace = _workspace_handle(tmp_path)
    (workspace.repo_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n",
        encoding="utf-8",
    )
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    git_status_command = _git_status_command(container.working_dir)
    lint_format_command = (
        f"cd {container.working_dir} && ruff format -- workers/codex_cli_worker.py"
    )
    lint_check_command = (
        f"cd {container.working_dir} && ruff check --fix -- workers/codex_cli_worker.py"
    )
    session = _FakeSession(
        {
            git_status_command: _command_result(
                git_status_command,
                output=" M workers/codex_cli_worker.py\0",
            ),
            lint_format_command: _command_result(lint_format_command, output="formatted"),
            lint_check_command: _command_result(lint_check_command, output="checked"),
        }
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done.")])
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda started_container, **_: session,
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-47-post-run-lint",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Run post lint step",
            )
        )
    )

    assert result.status == "success"
    assert [command.command for command in result.commands_run] == [
        "ruff format -- workers/codex_cli_worker.py",
        "ruff check --fix -- workers/codex_cli_worker.py",
    ]
    assert result.budget_usage is not None
    assert result.budget_usage["post_run_lint_format"]["ran"] is True
    assert result.budget_usage["post_run_lint_format"]["errors"] == []
    assert session.calls == [
        (git_status_command, 60),
        (lint_format_command, 60),
        (lint_check_command, 60),
        (git_status_command, 60),
    ]


def test_codex_cli_worker_scopes_and_injects_secrets(tmp_path: Path) -> None:
    """The worker should scope secrets for the container and pass all secrets for redaction."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-secrets",
        image="python:3.12-slim",
    )
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output="",
            )
        }
    )

    # Capture the secrets passed to the session factory
    captured_session_secrets: dict[str, str] | None = None

    def session_factory(started_container, *, secrets=None):
        nonlocal captured_session_secrets
        captured_session_secrets = secrets
        return session

    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done")])
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=workspace_manager,
        container_manager=container_manager,
        session_factory=session_factory,
    )

    request = WorkerRequest(
        session_id="session-secrets",
        repo_url="https://example.com/repo.git",
        branch="main",
        task_text="Check secrets",
        secrets={
            "GITHUB_TOKEN": "gh_secret",
            "OTHER_SECRET": "other",
        },
    )

    asyncio.run(worker.run(request))

    # verify container environment (scoped)
    start_request = container_manager.start_requests[0]
    # execute_github is in DEFAULT_TOOL_REGISTRY and requires GITHUB_TOKEN
    assert "GITHUB_TOKEN" in start_request.environment
    assert start_request.environment["GITHUB_TOKEN"] == "gh_secret"
    # OTHER_SECRET should NOT be in the container environment
    assert "OTHER_SECRET" not in start_request.environment

    # verify session secrets (all)
    assert captured_session_secrets == request.secrets


def test_codex_cli_worker_warns_when_legacy_tool_loop_mode_is_used(tmp_path: Path) -> None:
    """Tool-loop execution should emit a deprecation warning for observability."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-tool-loop-warning",
        image="python:3.12-slim",
    )
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")])
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

    with patch("workers.codex_cli_worker.logger.warning") as warning_logger:
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-tool-loop-warning",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Inspect only",
                    runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
                    worker_profile="codex-tool-loop-executor",
                )
            )
        )

    assert result.status == "success"
    warning_messages = [call.args[0] for call in warning_logger.call_args_list]
    assert any("tool_loop runtime mode is deprecated" in message for message in warning_messages)


def test_codex_cli_worker_rejects_non_execution_runtime_modes(tmp_path: Path) -> None:
    """Planner/reviewer runtime modes should fail fast for Codex execution worker."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-unsupported-mode",
        image="python:3.12-slim",
    )
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    worker = CodexCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=workspace_manager,
        container_manager=container_manager,
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                session_id="session-bad-runtime",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Do not execute",
                runtime_mode=WorkerRuntimeMode.PLANNER_ONLY,
            )
        )
    )

    assert result.status == "failure"
    assert result.failure_kind == "provider_error"
    assert "does not support runtime mode" in (result.summary or "")
    assert workspace_manager.cleanup_requests == [(workspace, False)]


def test_codex_cli_worker_handles_invalid_trusted_patterns(tmp_path: Path) -> None:
    """Worker initialization should be resilient to malformed regex patterns."""
    with patch("workers.codex_cli_worker.logger.warning") as mock_warning:
        worker = CodexCliWorker(
            runtime_adapter=_ScriptedAdapter([]),
            trusted_repo_patterns=["[invalid", "", "  ", "valid.*"],
        )
        assert len(worker.trusted_repo_patterns) == 1
        assert worker.trusted_repo_patterns[0].pattern == "valid.*"
        # Verify warning was logged for the invalid pattern
        assert mock_warning.called
        args = mock_warning.call_args[0]
        assert "Ignoring malformed trusted repository pattern" in args[0]


def test_codex_cli_worker_auto_enables_network(tmp_path: Path) -> None:
    """Worker should auto-enable network when tools require it and permission is granted."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    container_manager = _FakeContainerManager(container)

    # Use a tool that requires network
    from tools import EXECUTE_GITHUB_TOOL_NAME, ToolPermissionLevel

    worker = CodexCliWorker(
        runtime_adapter=_ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=container_manager,
        session_factory=lambda started_container, **_: _FakeSession(
            {
                _git_status_command(container.working_dir): _command_result(
                    _git_status_command(container.working_dir),
                    output="",
                )
            }
        ),
        tool_registry=DEFAULT_TOOL_REGISTRY,
    )

    asyncio.run(
        worker.run(
            WorkerRequest(
                task_text="create pr",
                repo_url="https://example.com/repo.git",
                tools=[EXECUTE_GITHUB_TOOL_NAME],
                constraints={"granted_permission": ToolPermissionLevel.NETWORKED_WRITE},
            )
        )
    )

    assert container_manager.start_requests[0].network_enabled is True
