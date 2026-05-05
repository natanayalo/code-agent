"""Unit tests for the Codex CLI worker."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

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
from workers.base import ArtifactReference
from workers.cli_runtime import CliRuntimeMessage, CliRuntimeStep
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


def _git_status_command(container_workdir: str = "/workspace/repo") -> str:
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
        workspace=workspace,
        container_name="sandbox-workspace-task-47",
        image="python:3.12-slim",
    )
    git_status_command = _git_status_command(container.working_dir)
    lint_format_command = "cd /workspace/repo && ruff format -- workers/codex_cli_worker.py"
    lint_check_command = "cd /workspace/repo && ruff check --fix -- workers/codex_cli_worker.py"
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


def test_codex_cli_worker_requests_higher_permission_for_blocked_commands(tmp_path: Path) -> None:
    """Permission-required runtime failures should map to a clear worker follow-up hint."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
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


def test_codex_cli_worker_scopes_and_injects_secrets(tmp_path: Path) -> None:
    """The worker should scope secrets for the container and pass all secrets for redaction."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
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


def test_codex_cli_worker_records_no_findings_self_review(tmp_path: Path) -> None:
    """Successful runs should persist an explicit no-findings self-review payload."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-self-review",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="final", final_output="Initial implementation complete."),
            CliRuntimeStep(
                kind="final",
                final_output=_review_result_json(
                    outcome="no_findings",
                    summary="Diff satisfies the task and no issues were found.",
                ),
            ),
        ]
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
                session_id="session-self-review-ok",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Implement a tiny change and validate it",
            )
        )
    )

    assert result.status == "success"
    assert result.review_result is not None
    assert result.review_result.outcome == "no_findings"
    assert result.budget_usage is not None
    assert adapter.calls[1] == []
    assert adapter.prompt_overrides[1] is not None
    assert "## Review Task" in adapter.prompt_overrides[1]


def test_codex_cli_worker_checks_cancel_before_self_review(tmp_path: Path) -> None:
    """The self-review coordinator should honor cancellation before review starts."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-cancel-review",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done")])
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

    with patch(
        "workers.codex_cli_worker.run_shared_self_review_fix_loop",
        return_value=(None, [], None, []),
    ) as review_loop:
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-cancel-before-review",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Complete task then review",
                )
            )
        )

    assert result.status == "success"
    assert review_loop.call_args is not None
    assert review_loop.call_args.kwargs["check_cancel_before_review"] is True


def test_codex_cli_worker_stops_container_when_setup_fails_after_start(tmp_path: Path) -> None:
    """Setup failures after container start should still clean up the started container."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
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


def test_codex_cli_worker_fixes_review_findings_with_bounded_retry(tmp_path: Path) -> None:
    """Actionable self-review findings should trigger a bounded follow-up fix loop."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-self-review-fix",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="final", final_output="Implemented the initial change."),
            CliRuntimeStep(
                kind="final",
                final_output=_review_result_json(
                    outcome="findings",
                    summary="A focused test is missing for the new behavior.",
                ),
            ),
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_bash",
                tool_input="printf 'assert True\\n' > tests/test_note.py",
            ),
            CliRuntimeStep(kind="final", final_output="Added the missing focused test."),
            CliRuntimeStep(
                kind="final",
                final_output=_review_result_json(
                    outcome="no_findings",
                    summary="Findings were addressed.",
                ),
            ),
        ]
    )
    session = _FakeSession(
        {
            "printf 'assert True\\n' > tests/test_note.py": _command_result(
                "printf 'assert True\\n' > tests/test_note.py",
                output="",
            ),
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output="?? tests/test_note.py\0",
            ),
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
                session_id="session-self-review-fix",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Add a minimal behavior and ensure it is covered by tests",
                budget={"max_iterations": 6},
            )
        )
    )

    assert result.status == "success"
    assert [command.command for command in result.commands_run] == [
        "printf 'assert True\\n' > tests/test_note.py"
    ]
    assert result.review_result is not None
    assert result.review_result.outcome == "no_findings"
    assert result.budget_usage is not None


def test_codex_cli_worker_accumulates_lint_artifacts_across_fix_loops(tmp_path: Path) -> None:
    """Lint artifacts from each lint pass should be preserved on the final worker result."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-self-review-lint-artifacts",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="final", final_output="Implemented initial change."),
            CliRuntimeStep(
                kind="final",
                final_output=_review_result_json(
                    outcome="findings",
                    summary="A follow-up fix is needed.",
                ),
            ),
            CliRuntimeStep(kind="final", final_output="Applied follow-up fix."),
            CliRuntimeStep(
                kind="final",
                final_output=_review_result_json(
                    outcome="no_findings",
                    summary="All findings are fixed.",
                ),
            ),
        ]
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

    with patch(
        "workers.codex_cli_worker.collect_changed_files_and_apply_post_run_lint_format",
        side_effect=[
            (
                ["workers/codex_cli_worker.py"],
                {
                    "ran": True,
                    "status": "passed",
                    "errors": [],
                    "commands": [],
                    "artifacts": [],
                },
                [
                    ArtifactReference(
                        name="lint-first",
                        uri="artifacts/lint-first.log",
                        artifact_type="log",
                    )
                ],
            ),
            (
                ["workers/codex_cli_worker.py", "workers/gemini_cli_worker.py"],
                {
                    "ran": True,
                    "status": "warning",
                    "errors": ["second pass warning"],
                    "commands": [],
                    "artifacts": [],
                },
                [
                    ArtifactReference(
                        name="lint-second",
                        uri="artifacts/lint-second.log",
                        artifact_type="log",
                    )
                ],
            ),
        ],
    ):
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-self-review-lint-artifacts",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Apply self-review fixes",
                )
            )
        )

    artifact_uris = {artifact.uri for artifact in result.artifacts}
    assert "artifacts/lint-first.log" in artifact_uris
    assert "artifacts/lint-second.log" in artifact_uris
    assert result.review_result is not None
    assert result.review_result.outcome == "no_findings"
    assert result.budget_usage is not None
    assert result.budget_usage["post_run_lint_format"]["status"] == "warning"
    assert result.budget_usage["post_run_lint_format"]["errors"] == ["second pass warning"]


def test_codex_cli_worker_respects_zero_fix_retry_limit(tmp_path: Path) -> None:
    """When fix retries are disabled, findings should be reported without re-entry."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-self-review-zero-fix",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="final", final_output="Implemented initial change."),
            CliRuntimeStep(
                kind="final",
                final_output=_review_result_json(
                    outcome="findings",
                    summary="A follow-up assertion is still missing.",
                ),
            ),
        ]
    )
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output=" M note.txt\0",
            ),
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
                session_id="session-self-review-no-fix",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Perform a quick change",
                constraints={"self_review_max_fix_iterations": 0},
            )
        )
    )

    assert result.status == "success"
    assert result.review_result is not None
    assert result.review_result.outcome == "findings"
    assert result.commands_run == []
    assert result.budget_usage is not None


def test_codex_cli_worker_allows_opt_out_of_self_review(tmp_path: Path) -> None:
    """Self-review should be skipped when explicitly disabled via constraints."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name="sandbox-workspace-task-self-review-skip",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done quickly.")])
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output="",
            ),
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
                session_id="session-self-review-skip",
                repo_url="https://example.com/repo.git",
                branch="main",
                task_text="Do a quick read-only check",
                constraints={"skip_self_review": True},
            )
        )
    )

    assert result.status == "success"
    assert result.review_result is None
    assert result.budget_usage is not None


def test_codex_cli_worker_runs_native_agent_mode_when_requested(tmp_path: Path) -> None:
    """Codex native mode should invoke one-shot runner and skip tool-loop container setup."""
    adapter = _ScriptedAdapter([])
    # Regression guard: adapter defaults must not force native mode into read-only.
    adapter.sandbox_mode = "read-only"
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
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
    )

    with patch(
        "workers.codex_cli_worker.run_native_agent",
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


def test_codex_cli_worker_rejects_non_execution_runtime_modes(tmp_path: Path) -> None:
    """Planner/reviewer runtime modes should fail fast for Codex execution worker."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
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


def test_codex_cli_worker_native_mode_honors_read_only_constraint(tmp_path: Path) -> None:
    """Native mode should force read-only sandbox when the task constraint requires it."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
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
        "workers.codex_cli_worker.run_native_agent",
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
