"""Unit tests for the Codex CLI worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerRequest,
    DockerShellCommandResult,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
)
from workers import CodexCliWorker, ReviewResult, WorkerRequest
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


def _make_default_session(container: DockerSandboxContainer) -> _FakeSession:
    return _FakeSession(
        {
            _git_status_command(container.working_dir): _command_result(
                _git_status_command(container.working_dir),
                output="",
            )
        }
    )


def _make_lint_result(status: str, errors: list[str]) -> dict[str, Any]:
    return {
        "ran": True,
        "status": status,
        "errors": errors,
        "commands": [],
        "artifacts": [],
    }


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


def test_codex_cli_worker_records_no_findings_self_review(tmp_path: Path) -> None:
    """Successful runs should persist an explicit no-findings self-review payload."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
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
                output=" M main.py\0",
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
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-cancel-review",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done")])
    session = _make_default_session(container)
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda started_container, **_: session,
    )

    with patch(
        "workers.runtime_executor.run_shared_self_review_fix_loop",
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


def test_codex_cli_worker_fixes_review_findings_with_bounded_retry(tmp_path: Path) -> None:
    """Actionable self-review findings should trigger a bounded follow-up fix loop."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        working_dir="/workspace",
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
        working_dir="/workspace",
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
    session = _make_default_session(container)
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda started_container, **_: session,
    )

    with patch(
        "workers.runtime_executor.collect_changed_files_and_apply_post_run_lint_format",
        side_effect=[
            (
                ["workers/codex_cli_worker.py"],
                _make_lint_result("passed", []),
                [
                    ArtifactReference(
                        name="lint-first", uri="artifacts/lint-first.log", artifact_type="log"
                    )
                ],
            ),
            (
                ["workers/codex_cli_worker.py", "workers/gemini_cli_worker.py"],
                _make_lint_result("warning", ["second pass warning"]),
                [
                    ArtifactReference(
                        name="lint-second", uri="artifacts/lint-second.log", artifact_type="log"
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
        working_dir="/workspace",
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
        working_dir="/workspace",
        workspace=workspace,
        container_name="sandbox-workspace-task-self-review-skip",
        image="python:3.12-slim",
    )
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done quickly.")])
    session = _make_default_session(container)
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
