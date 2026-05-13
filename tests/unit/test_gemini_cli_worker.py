"""Unit tests for the Gemini CLI worker."""

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
    WorkspaceManagerError,
)
from tools import DEFAULT_TOOL_REGISTRY
from workers import GeminiCliWorker, WorkerRequest
from workers.base import ArtifactReference
from workers.cli_runtime import CliRuntimeMessage, CliRuntimeSettings, CliRuntimeStep
from workers.gemini_cli_worker import _workspace_task_id
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
        container_name="test-gemini-container",
        image="python:3.12-slim",
        workspace=workspace,
    )


def _git_status_command(container_workdir: str = "/workspace/repo") -> str:
    return f"git -C {container_workdir} status --porcelain=v1 -z --untracked-files=all"


def test_gemini_cli_worker_runs_successfully(tmp_path: Path) -> None:
    """Worker should return a success result when the adapter finishes cleanly."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    git_status_result = DockerShellCommandResult(
        command=_git_status_command(container.working_dir),
        exit_code=0,
        output="",
        duration_seconds=0.0,
    )
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): git_status_result,
        }
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="final", final_output="All done."),
        ]
    )
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: session,
        tool_registry=DEFAULT_TOOL_REGISTRY,
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(
                task_text="List files",
                repo_url="https://github.com/example/repo.git",
            )
        )
    )

    assert result.status == "success"
    assert "All done." in (result.summary or "")
    assert session.closed is True


def test_gemini_cli_worker_errors_without_repo_url(tmp_path: Path) -> None:
    """Worker should refuse to run when repo_url is absent."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: _FakeSession({}),
    )

    result = asyncio.run(worker.run(WorkerRequest(task_text="do something")))

    assert result.status == "error"
    assert "repo_url" in (result.summary or "")
    assert result.next_action_hint == "provide_repo_url"


def test_gemini_validate_request_phase_returns_none_for_valid_repo_url(tmp_path: Path) -> None:
    """The request-validation phase should pass through when repo_url is present."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: _FakeSession({}),
    )

    assert (
        worker._validate_request(
            WorkerRequest(task_text="do something", repo_url="https://example.com/repo.git")
        )
        is None
    )


def test_gemini_cli_worker_errors_when_workspace_provisioning_fails(tmp_path: Path) -> None:
    """Worker should return an error result when workspace creation raises."""

    class _FailingWorkspaceManager:
        def create_workspace(self, request: object) -> WorkspaceHandle:
            raise WorkspaceManagerError("disk full")

        def cleanup_workspace(self, workspace: WorkspaceHandle, *, succeeded: bool) -> bool:
            return False

    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FailingWorkspaceManager(),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: _FakeSession({}),
    )

    result = asyncio.run(
        worker.run(WorkerRequest(task_text="do something", repo_url="https://example.com/repo.git"))
    )

    assert result.status == "error"
    assert "disk full" in (result.summary or "")
    assert result.next_action_hint == "inspect_worker_configuration"


def test_gemini_cli_worker_workspace_task_id_uses_gemini_prefix(tmp_path: Path) -> None:
    """Workspace task IDs should carry the gemini-cli prefix."""
    request = WorkerRequest(task_text="build the feature", repo_url="https://example.com/repo")
    task_id = _workspace_task_id(request)
    assert task_id.startswith("gemini-cli-")


def test_gemini_cli_worker_uses_session_id_in_workspace_task_id() -> None:
    """When session_id is present it should be used over task_text for the workspace ID."""
    request = WorkerRequest(
        task_text="build the feature",
        session_id="session-abc-123",
        repo_url="https://example.com/repo",
    )
    task_id = _workspace_task_id(request)
    assert task_id.startswith("gemini-cli-")
    assert "session" in task_id


def test_gemini_cli_worker_cleanup_applied_on_success(tmp_path: Path) -> None:
    """When the workspace is deleted on success the artifact list should be cleared."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)

    class _DeletingManager:
        def __init__(self, ws: WorkspaceHandle) -> None:
            self.workspace = ws

        def create_workspace(self, request: object) -> WorkspaceHandle:
            return self.workspace

        def cleanup_workspace(self, workspace: WorkspaceHandle, *, succeeded: bool) -> bool:
            return True  # signals deleted

    session = _FakeSession(
        {
            _git_status_command(container.working_dir): DockerShellCommandResult(
                command=_git_status_command(container.working_dir),
                exit_code=0,
                output="",
                duration_seconds=0.0,
            )
        }
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done.")])
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_DeletingManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: session,
        tool_registry=DEFAULT_TOOL_REGISTRY,
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=True, retain_on_failure=False),
    )

    result = asyncio.run(
        worker.run(WorkerRequest(task_text="t", repo_url="https://example.com/repo"))
    )

    assert result.artifacts == []
    assert "cleaned up" in (result.summary or "").lower()


def test_gemini_cli_worker_self_review_with_findings(tmp_path: Path) -> None:
    """Worker should do a fix pass when self-review has findings."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): DockerShellCommandResult(
                command=_git_status_command(container.working_dir),
                exit_code=0,
                output="",
                duration_seconds=0.0,
            )
        }
    )

    findings_json = json.dumps(
        {
            "summary": "found issues",
            "confidence": 1.0,
            "outcome": "findings",
            "findings": [
                {
                    "severity": "low",
                    "category": "style",
                    "confidence": 1.0,
                    "file_path": "a.py",
                    "line_start": 1,
                    "line_end": 1,
                    "title": "t",
                    "why_it_matters": "w",
                    "evidence": "e",
                    "suggested_fix": "f",
                }
            ],
        }
    )

    adapter = _ScriptedAdapter(
        [
            # main loop completes
            CliRuntimeStep(kind="final", final_output="Done initial."),
            # self review returns findings
            CliRuntimeStep(kind="final", final_output=findings_json),
            # fix loop completes
            CliRuntimeStep(kind="final", final_output="Done fix."),
            # second self review returns no findings
            CliRuntimeStep(
                kind="final",
                final_output=json.dumps(
                    {
                        "summary": "all fixed",
                        "confidence": 1.0,
                        "outcome": "no_findings",
                        "findings": [],
                    }
                ),
            ),
        ]
    )
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: session,
        tool_registry=DEFAULT_TOOL_REGISTRY,
    )

    result = asyncio.run(
        worker.run(WorkerRequest(task_text="task", repo_url="https://example.com/repo"))
    )

    assert result.status == "success"
    assert "Done fix." in (result.summary or "")
    assert result.review_result is not None
    assert result.review_result.outcome == "no_findings"


def test_gemini_cli_worker_checks_cancel_before_self_review(tmp_path: Path) -> None:
    """The self-review coordinator should honor cancellation before review starts."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): DockerShellCommandResult(
                command=_git_status_command(container.working_dir),
                exit_code=0,
                output="",
                duration_seconds=0.0,
            )
        }
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done")])
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: session,
        tool_registry=DEFAULT_TOOL_REGISTRY,
    )

    with patch(
        "workers.gemini_cli_worker.run_shared_self_review_fix_loop",
        return_value=(None, [], None, []),
    ) as review_loop:
        result = asyncio.run(
            worker.run(WorkerRequest(task_text="task", repo_url="https://example.com/repo"))
        )

    assert result.status == "success"
    assert review_loop.call_args is not None
    assert review_loop.call_args.kwargs["check_cancel_before_review"] is True


def test_gemini_cli_worker_stops_container_when_setup_fails_after_start(tmp_path: Path) -> None:
    """Setup failures after container start should still clean up the started container."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    container_manager = _FakeContainerManager(container)

    def _failing_session_factory(_, **__):
        raise OSError("session init failed")

    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=container_manager,
        session_factory=_failing_session_factory,
    )

    result = asyncio.run(
        worker.run(
            WorkerRequest(task_text="task", repo_url="https://example.com/repo", branch="main")
        )
    )

    assert result.status == "error"
    assert "session init failed" in (result.summary or "")
    assert container_manager.stop_requests == [container]


def test_gemini_cli_worker_accumulates_lint_artifacts_across_fix_loops(tmp_path: Path) -> None:
    """Lint artifacts from each lint pass should be preserved on the final worker result."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): DockerShellCommandResult(
                command=_git_status_command(container.working_dir),
                exit_code=0,
                output="",
                duration_seconds=0.0,
            )
        }
    )

    findings_json = json.dumps(
        {
            "summary": "found issues",
            "confidence": 1.0,
            "outcome": "findings",
            "findings": [
                {
                    "severity": "low",
                    "category": "style",
                    "confidence": 1.0,
                    "file_path": "a.py",
                    "line_start": 1,
                    "line_end": 1,
                    "title": "t",
                    "why_it_matters": "w",
                    "evidence": "e",
                    "suggested_fix": "f",
                }
            ],
        }
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="final", final_output="Done initial."),
            CliRuntimeStep(kind="final", final_output=findings_json),
            CliRuntimeStep(kind="final", final_output="Done fix."),
            CliRuntimeStep(
                kind="final",
                final_output=json.dumps(
                    {
                        "summary": "all fixed",
                        "confidence": 1.0,
                        "outcome": "no_findings",
                        "findings": [],
                    }
                ),
            ),
        ]
    )
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: session,
        tool_registry=DEFAULT_TOOL_REGISTRY,
    )

    with patch(
        "workers.gemini_cli_worker.collect_changed_files_and_apply_post_run_lint_format",
        side_effect=[
            (
                ["workers/gemini_cli_worker.py"],
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
                ["workers/gemini_cli_worker.py", "workers/openrouter_cli_worker.py"],
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
            worker.run(WorkerRequest(task_text="task", repo_url="https://example.com/repo"))
        )

    artifact_uris = {artifact.uri for artifact in result.artifacts}
    assert "artifacts/lint-first.log" in artifact_uris
    assert "artifacts/lint-second.log" in artifact_uris
    assert result.budget_usage is not None
    assert result.budget_usage["post_run_lint_format"]["status"] == "warning"
    assert result.budget_usage["post_run_lint_format"]["errors"] == ["second pass warning"]


def test_gemini_cli_worker_self_review_exhausts_budget(tmp_path: Path) -> None:
    """Worker should mark as failure if budget is exceeded during fix loop."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): DockerShellCommandResult(
                command=_git_status_command(container.working_dir),
                exit_code=0,
                output="",
                duration_seconds=0.0,
            )
        }
    )

    findings_json = json.dumps(
        {
            "summary": "found issues",
            "confidence": 1.0,
            "outcome": "findings",
            "findings": [
                {
                    "severity": "low",
                    "category": "style",
                    "confidence": 1.0,
                    "file_path": "a.py",
                    "line_start": 1,
                    "line_end": 1,
                    "title": "t",
                    "why_it_matters": "w",
                    "evidence": "e",
                    "suggested_fix": "f",
                }
            ],
        }
    )

    adapter = _ScriptedAdapter(
        [
            # main loop completes
            CliRuntimeStep(kind="final", final_output="Done initial."),
            # self review returns findings
            CliRuntimeStep(kind="final", final_output=findings_json),
            # but budget is 0 so it won't even call the adapter for fix loop!
        ]
    )
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: session,
        tool_registry=DEFAULT_TOOL_REGISTRY,
        runtime_settings=CliRuntimeSettings(max_iterations=1),  # exactly enough for initial run
    )

    result = asyncio.run(
        worker.run(WorkerRequest(task_text="task", repo_url="https://example.com/repo"))
    )

    assert result.status == "failure"
    assert "exhausted its remaining budget" in (result.summary or "")


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
    )

    with patch(
        "workers.gemini_cli_worker.run_native_agent",
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
    assert command[command.index("--approval-mode") + 1] == "default"
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
        "workers.gemini_cli_worker.run_native_agent",
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


def test_gemini_cli_worker_warns_when_legacy_tool_loop_mode_is_used(tmp_path: Path) -> None:
    """Tool-loop execution should emit a deprecation warning for observability."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")])
    session = _FakeSession(
        {
            _git_status_command(container.working_dir): DockerShellCommandResult(
                command=_git_status_command(container.working_dir),
                exit_code=0,
                output="",
                duration_seconds=0.0,
            ),
        }
    )
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=workspace_manager,
        container_manager=container_manager,
        session_factory=lambda _, **__: session,
    )

    with patch("workers.gemini_cli_worker.logger.warning") as warning_logger:
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-gemini-tool-loop-warning",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Inspect only",
                    runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
                    worker_profile="gemini-tool-loop-executor",
                )
            )
        )

    assert result.status == "success"
    warning_messages = [call.args[0] for call in warning_logger.call_args_list]
    assert any("tool_loop runtime mode is deprecated" in message for message in warning_messages)


def test_gemini_cli_worker_supports_specialist_runtime_modes(tmp_path: Path) -> None:
    """Specialist runtime modes (reviewer, planner) should be supported by Gemini worker."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=workspace_manager,
        container_manager=container_manager,
    )
    native_result = NativeAgentRunResult(
        status="success",
        summary="Specialist run completed.",
        command="gemini ...",
        exit_code=0,
        duration_seconds=0.5,
        timed_out=False,
        final_message="OK",
        stdout="{}",
        stderr="",
    )

    with patch(
        "workers.gemini_cli_worker.run_native_agent",
        return_value=native_result,
    ) as _:
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-specialist",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="Review this code",
                    runtime_mode=WorkerRuntimeMode.REVIEWER_ONLY,
                )
            )
        )

    assert result.status == "success"


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
    assert "CRITICAL: Your final response MUST be a single JSON object" in prompt


def test_gemini_cli_worker_runtime_error_is_mapped_to_workspace_error(tmp_path: Path) -> None:
    """RuntimeError from setup/runtime phases should map to structured worker errors."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    workspace_manager = _FakeWorkspaceManager(workspace)
    container_manager = _FakeContainerManager(container)
    worker = GeminiCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=workspace_manager,
        container_manager=container_manager,
        session_factory=lambda _, **__: _FakeSession({}),
    )

    with patch.object(worker, "_setup_runtime_phase", side_effect=RuntimeError("adapter exploded")):
        result = asyncio.run(
            worker.run(
                WorkerRequest(
                    session_id="session-runtime-error",
                    repo_url="https://example.com/repo.git",
                    branch="main",
                    task_text="trigger runtime error",
                )
            )
        )

    assert result.status == "error"
    assert "adapter exploded" in (result.summary or "")
    assert result.next_action_hint == "inspect_worker_configuration"
