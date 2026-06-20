"""Unit tests for the Gemini CLI worker."""

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
from tools import DEFAULT_TOOL_REGISTRY
from workers import GeminiCliWorker, WorkerRequest
from workers.base import ArtifactReference
from workers.cli_runtime import CliRuntimeMessage, CliRuntimeSettings, CliRuntimeStep


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
        working_dir="/workspace",
        container_name="test-gemini-container",
        image="python:3.12-slim",
        workspace=workspace,
    )


def _git_status_command(container_workdir: str = "/workspace/repo") -> str:
    return f"git -C {container_workdir} status --porcelain=v1 -z --untracked-files=all"


def _make_default_session(container: DockerSandboxContainer) -> _FakeSession:
    return _FakeSession(
        {
            _git_status_command(container.working_dir): DockerShellCommandResult(
                command=_git_status_command(container.working_dir),
                exit_code=0,
                output=" M main.py\0",
                duration_seconds=0.0,
            )
        }
    )


def _make_findings_json() -> str:
    return json.dumps(
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


def test_gemini_cli_worker_self_review_with_findings(tmp_path: Path) -> None:
    """Worker should do a fix pass when self-review has findings."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    session = _make_default_session(container)
    findings_json = _make_findings_json()

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
    session = _make_default_session(container)
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="Done")])
    worker = GeminiCliWorker(
        runtime_adapter=adapter,
        workspace_manager=_FakeWorkspaceManager(workspace),
        container_manager=_FakeContainerManager(container),
        session_factory=lambda _, **__: session,
        tool_registry=DEFAULT_TOOL_REGISTRY,
    )

    with patch(
        "workers.gemini_cli_worker_runtime.run_shared_self_review_fix_loop",
        return_value=(None, [], None, []),
    ) as review_loop:
        result = asyncio.run(
            worker.run(WorkerRequest(task_text="task", repo_url="https://example.com/repo"))
        )

    assert result.status == "success"
    assert review_loop.call_args is not None
    assert review_loop.call_args.kwargs["check_cancel_before_review"] is True


def test_gemini_cli_worker_accumulates_lint_artifacts_across_fix_loops(tmp_path: Path) -> None:
    """Lint artifacts from each lint pass should be preserved on the final worker result."""
    workspace = _make_workspace(tmp_path)
    container = _make_container(workspace)
    session = _make_default_session(container)
    findings_json = _make_findings_json()
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
        "workers.gemini_cli_worker_runtime.collect_changed_files_and_apply_post_run_lint_format",
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
                        name="lint-first", uri="artifacts/lint-first.log", artifact_type="log"
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
                        name="lint-second", uri="artifacts/lint-second.log", artifact_type="log"
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
    session = _make_default_session(container)
    findings_json = _make_findings_json()

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
