"""Unit tests for the OpenRouter CLI worker."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerRequest,
    DockerShellCommandResult,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManagerError,
)
from tools import DEFAULT_TOOL_REGISTRY
from workers import OpenRouterCliWorker, WorkerRequest
from workers.base import ArtifactReference
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
    repo_path.mkdir(exist_ok=True)
    (repo_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    return WorkspaceHandle(
        workspace_id="ws-openrouter-test",
        task_id="openrouter-cli-test-task",
        workspace_path=tmp_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )


def _make_container(workspace: WorkspaceHandle) -> DockerSandboxContainer:
    return DockerSandboxContainer(
        container_name="test-openrouter-container",
        image="python:3.12-slim",
        workspace=workspace,
    )


def _git_status_command(container_workdir: str = "/workspace/repo") -> str:
    return f"git -C {container_workdir} status --porcelain=v1 -z --untracked-files=all"


def test_openrouter_cli_worker_runs_successfully(tmp_path: Path) -> None:
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
    worker = OpenRouterCliWorker(
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


def test_openrouter_cli_worker_errors_without_repo_url(tmp_path: Path) -> None:
    """Worker should refuse to run when repo_url is absent."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    worker = OpenRouterCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: _FakeSession({}),
    )

    result = asyncio.run(worker.run(WorkerRequest(task_text="do something")))

    assert result.status == "error"
    assert "repo_url" in (result.summary or "")
    assert result.next_action_hint == "provide_repo_url"


def test_openrouter_cli_worker_errors_when_workspace_provisioning_fails(tmp_path: Path) -> None:
    """Worker should return an error result when workspace creation raises."""

    class _FailingWorkspaceManager:
        def create_workspace(self, request: object) -> WorkspaceHandle:
            raise WorkspaceManagerError("disk full")

        def cleanup_workspace(self, workspace: WorkspaceHandle, *, succeeded: bool) -> bool:
            return False

    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    worker = OpenRouterCliWorker(
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


def test_openrouter_cli_worker_workspace_task_id_uses_openrouter_prefix(tmp_path: Path) -> None:
    """Workspace task IDs should carry the openrouter-cli prefix."""
    from workers.openrouter_cli_worker import _workspace_task_id

    request = WorkerRequest(task_text="build the feature", repo_url="https://example.com/repo")
    task_id = _workspace_task_id(request)
    assert task_id.startswith("openrouter-cli-")


def test_openrouter_cli_worker_uses_session_id_in_workspace_task_id() -> None:
    """When session_id is present it should be used over task_text for the workspace ID."""
    from workers.openrouter_cli_worker import _workspace_task_id

    request = WorkerRequest(
        task_text="build the feature",
        session_id="session-abc-123",
        repo_url="https://example.com/repo",
    )
    task_id = _workspace_task_id(request)
    assert task_id.startswith("openrouter-cli-")
    assert "session" in task_id


def test_openrouter_cli_worker_cleanup_applied_on_success(tmp_path: Path) -> None:
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
    worker = OpenRouterCliWorker(
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


def test_openrouter_cli_worker_self_review_with_findings(tmp_path: Path) -> None:
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
    import json

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
    worker = OpenRouterCliWorker(
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


def test_openrouter_cli_worker_accumulates_lint_artifacts_across_fix_loops(tmp_path: Path) -> None:
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
    import json

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
    worker = OpenRouterCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: session,
        tool_registry=DEFAULT_TOOL_REGISTRY,
    )

    with patch(
        "workers.openrouter_cli_worker.collect_changed_files_and_apply_post_run_lint_format",
        side_effect=[
            (
                ["workers/openrouter_cli_worker.py"],
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
                ["workers/openrouter_cli_worker.py", "workers/gemini_cli_worker.py"],
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


def test_openrouter_cli_worker_self_review_exhausts_budget(tmp_path: Path) -> None:
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
    import json

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
    from workers.cli_runtime import CliRuntimeSettings

    worker = OpenRouterCliWorker(
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


def test_openrouter_next_action_hint() -> None:
    from workers.cli_runtime import CliRuntimeBudgetLedger, CliRuntimeExecutionResult
    from workers.openrouter_cli_worker import _next_action_hint

    def make_exec(stop_reason):
        return CliRuntimeExecutionResult(
            status="failure",
            summary="failed",
            stop_reason=stop_reason,
            commands_run=[],
            messages=[],
            permission_decision=None,
            budget_ledger=CliRuntimeBudgetLedger(max_iterations=10),
        )

    assert _next_action_hint(make_exec("permission_required")) == "request_higher_permission"
    assert _next_action_hint(make_exec("max_iterations")) == "increase_budget_or_reduce_scope"
    assert _next_action_hint(make_exec("context_window")) == "reduce_context_or_scope"
    assert _next_action_hint(make_exec("adapter_error")) == "inspect_worker_configuration"
    assert _next_action_hint(make_exec("shell_error")) == "inspect_workspace_artifacts"


def test_openrouter_cleanup_workspace_error(tmp_path, caplog) -> None:
    from sandbox import WorkspaceManagerError
    from workers.openrouter_cli_worker import OpenRouterCliWorker, WorkerRequest

    class _ErrorWorkspaceManager:
        def cleanup_workspace(self, workspace, *, succeeded):
            raise WorkspaceManagerError("fail")

    worker = OpenRouterCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_ErrorWorkspaceManager(),
        container_manager=_FakeContainerManager(_make_container(_make_workspace(tmp_path))),
    )
    ws = _make_workspace(tmp_path)
    res = worker._cleanup_workspace(
        WorkerRequest(task_text="t"),
        ws,
        workspace_task_id="x",
        run_succeeded=True,
    )
    assert res is False
    assert "failed to clean up workspace" in caplog.text


def test_openrouter_stop_container_error(tmp_path, caplog) -> None:
    from sandbox import DockerSandboxContainerError
    from workers.openrouter_cli_worker import OpenRouterCliWorker

    class _ErrorContainerManager:
        def stop(self, container):
            raise DockerSandboxContainerError("stop fail")

    worker = OpenRouterCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        container_manager=_ErrorContainerManager(),
    )
    ws = _make_workspace(tmp_path)
    cont = _make_container(ws)
    worker._stop_container(cont)
    assert "failed to stop the persistent container" in caplog.text


def test_openrouter_close_session_error(caplog) -> None:
    from workers.openrouter_cli_worker import OpenRouterCliWorker

    class _ErrorSession:
        def close(self):
            raise OSError("close fail")

    worker = OpenRouterCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
    )
    worker._close_session(_ErrorSession())
    assert "failed to close the persistent shell session" in caplog.text


def test_openrouter_run_sync_container_start_error(tmp_path) -> None:
    from sandbox import DockerSandboxContainerError
    from workers.openrouter_cli_worker import OpenRouterCliWorker

    workspace = _make_workspace(tmp_path)

    class _ErrorContainerManager:
        def start(self, request):
            raise DockerSandboxContainerError("start fail")

        def stop(self, container):
            pass

    worker = OpenRouterCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_ErrorContainerManager(),
    )
    result = worker._run_sync(WorkerRequest(task_text="task", repo_url="https://x.com/r"))
    assert result.status == "error"
    assert "start fail" in result.summary
    assert result.failure_kind == "sandbox_infra"


def test_openrouter_run_cancellation(tmp_path) -> None:
    import asyncio

    from workers.openrouter_cli_worker import OpenRouterCliWorker

    workspace = _make_workspace(tmp_path)

    class _HangingWorkspaceManager:
        def create_workspace(self, request):
            import time

            time.sleep(0.5)
            return workspace

        def cleanup_workspace(self, workspace, *, succeeded):
            return True

    worker = OpenRouterCliWorker(
        runtime_adapter=_ScriptedAdapter([]),
        workspace_manager=_HangingWorkspaceManager(),
    )

    async def cancel_test():
        task = asyncio.create_task(
            worker.run(WorkerRequest(task_text="t", repo_url="https://x.com/r"))
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(cancel_test())
