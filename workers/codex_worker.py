"""Codex worker adapter backed by sandboxed toy repo execution."""

from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import Path
from typing import Literal

from sandbox import (
    DockerSandboxCommand,
    DockerSandboxRunner,
    DockerSandboxRunnerError,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceRequest,
)
from workers.base import (
    ArtifactReference,
    TestResult,
    Worker,
    WorkerCommand,
    WorkerRequest,
    WorkerResult,
)

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ROOT = Path("/tmp/code-agent-workspaces")
DEFAULT_SANDBOX_TIMEOUT_SECONDS = 300

_TOY_TASK_SCRIPT = """
from pathlib import Path
import json
import os

repo = Path("/workspace/repo")
report_path = repo / ".code-agent" / "codex-worker-report.md"
report_path.parent.mkdir(parents=True, exist_ok=True)
top_level_entries = sorted(path.name for path in repo.iterdir())
memory_context = json.loads(os.environ["MEMORY_CONTEXT_JSON"])
constraints = json.loads(os.environ["CONSTRAINTS_JSON"])
budget = json.loads(os.environ["BUDGET_JSON"])

report_lines = [
    "# Codex Worker Report",
    "",
    f"Task: {os.environ['TASK_TEXT']}",
    f"Session: {os.environ['SESSION_ID']}",
    f"Repo URL: {os.environ['REPO_URL']}",
    f"Branch: {os.environ['BRANCH']}",
    "",
    "Top-level repo entries:",
]
report_lines.extend(f"- {entry}" for entry in top_level_entries[:20] or ["(none)"])
report_lines.extend(
    [
        "",
        "Memory context:",
        json.dumps(memory_context, indent=2, sort_keys=True),
        "",
        "Constraints:",
        json.dumps(constraints, indent=2, sort_keys=True),
        "",
        "Budget:",
        json.dumps(budget, indent=2, sort_keys=True),
    ]
)
report_path.write_text("\\n".join(report_lines) + "\\n", encoding="utf-8")
print(f"Wrote {report_path.relative_to(repo)}")
"""


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for sandbox bookkeeping."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _workspace_task_id(request: WorkerRequest) -> str:
    """Build a readable workspace task identifier from the worker request."""
    source = request.session_id or request.task_text
    return f"codex-{_slugify(source)}"


def _serialize_context(value: dict[str, object]) -> str:
    """Serialize worker context into an inspectable environment payload."""
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _workspace_artifacts(
    workspace: WorkspaceHandle,
    *,
    sandbox_artifacts: list[ArtifactReference] | None = None,
) -> list[ArtifactReference]:
    """Build absolute artifact references for a retained workspace."""
    artifacts = [
        ArtifactReference(
            name="workspace",
            uri=str(workspace.workspace_path),
            artifact_type="workspace",
        )
    ]
    if sandbox_artifacts:
        artifacts.extend(sandbox_artifacts)
    return artifacts


class CodexWorker(Worker):
    """Execute a deterministic toy repo task through the sandbox stack."""

    def __init__(
        self,
        *,
        workspace_manager: WorkspaceManager | None = None,
        sandbox_runner: DockerSandboxRunner | None = None,
        workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
        cleanup_policy: WorkspaceCleanupPolicy | None = None,
        timeout_seconds: int = DEFAULT_SANDBOX_TIMEOUT_SECONDS,
    ) -> None:
        self.cleanup_policy = cleanup_policy or WorkspaceCleanupPolicy(
            delete_on_success=False,
            retain_on_failure=True,
        )
        self.workspace_manager = workspace_manager or WorkspaceManager(
            workspace_root,
            cleanup_policy=self.cleanup_policy,
        )
        self.sandbox_runner = sandbox_runner or DockerSandboxRunner()
        self.timeout_seconds = timeout_seconds

    def run(self, request: WorkerRequest) -> WorkerResult:
        """Provision a workspace, run the toy task, and return a typed result."""
        if request.repo_url is None:
            return WorkerResult(
                status="error",
                summary="CodexWorker requires repo_url to provision a sandbox workspace.",
                next_action_hint="provide_repo_url",
            )

        workspace_task_id = _workspace_task_id(request)
        logger.info(
            "Starting Codex worker run",
            extra={
                "session_id": request.session_id,
                "repo_url": request.repo_url,
                "branch": request.branch,
                "workspace_task_id": workspace_task_id,
            },
        )

        try:
            workspace = self.workspace_manager.create_workspace(
                WorkspaceRequest(
                    task_id=workspace_task_id,
                    repo_url=request.repo_url,
                    branch=request.branch,
                    cleanup_policy=self.cleanup_policy,
                )
            )
        except WorkspaceManagerError as exc:
            logger.exception(
                "Codex worker failed to provision workspace",
                extra={"session_id": request.session_id, "workspace_task_id": workspace_task_id},
            )
            return WorkerResult(
                status="error",
                summary=f"CodexWorker failed to provision a workspace: {exc}",
                next_action_hint="inspect_worker_configuration",
            )

        try:
            sandbox_result = self.sandbox_runner.run(
                DockerSandboxCommand(
                    workspace=workspace,
                    command=["python3", "-c", _TOY_TASK_SCRIPT],
                    environment={
                        "TASK_TEXT": request.task_text,
                        "SESSION_ID": request.session_id or "unknown",
                        "REPO_URL": request.repo_url,
                        "BRANCH": request.branch or "default",
                        "MEMORY_CONTEXT_JSON": _serialize_context(request.memory_context),
                        "CONSTRAINTS_JSON": _serialize_context(request.constraints),
                        "BUDGET_JSON": _serialize_context(request.budget),
                    },
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except DockerSandboxRunnerError as exc:
            logger.exception(
                "Codex worker sandbox execution failed",
                extra={
                    "session_id": request.session_id,
                    "workspace_id": workspace.workspace_id,
                    "workspace_task_id": workspace_task_id,
                },
            )
            return WorkerResult(
                status="error",
                summary=f"CodexWorker sandbox execution failed: {exc}",
                artifacts=_workspace_artifacts(workspace),
                next_action_hint="inspect_workspace_artifacts",
            )

        command_summary = shlex.join(sandbox_result.command)
        sandbox_artifacts = [
            ArtifactReference(
                name=artifact.name,
                uri=str((workspace.workspace_path / artifact.uri).resolve()),
                artifact_type=artifact.artifact_type,
            )
            for artifact in sandbox_result.artifacts
        ]
        status: Literal["success", "failure"] = (
            "success" if sandbox_result.exit_code == 0 else "failure"
        )
        summary = (
            "CodexWorker completed a sandboxed toy repo task and retained the workspace."
            if status == "success"
            else f"CodexWorker toy repo task exited with code {sandbox_result.exit_code}."
        )
        result = WorkerResult(
            status=status,
            summary=summary,
            commands_run=[
                WorkerCommand(
                    command=command_summary,
                    exit_code=sandbox_result.exit_code,
                    duration_seconds=sandbox_result.duration_seconds,
                )
            ],
            files_changed=sandbox_result.files_changed,
            test_results=[
                TestResult(
                    name="codex_worker_toy_task",
                    status="passed" if sandbox_result.exit_code == 0 else "failed",
                    details=sandbox_result.stdout.strip() or sandbox_result.stderr.strip() or None,
                )
            ],
            artifacts=_workspace_artifacts(workspace, sandbox_artifacts=sandbox_artifacts),
            next_action_hint="inspect_workspace_artifacts",
        )

        logger.info(
            "Codex worker finished",
            extra={
                "session_id": request.session_id,
                "workspace_id": workspace.workspace_id,
                "status": result.status,
                "files_changed_count": len(result.files_changed),
                "artifact_count": len(result.artifacts),
            },
        )
        return result
