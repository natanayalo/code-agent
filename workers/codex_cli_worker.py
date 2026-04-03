"""Codex CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerError,
    DockerSandboxContainerManager,
    DockerSandboxContainerRequest,
    DockerShellSession,
    DockerShellSessionError,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceRequest,
)
from sandbox.workspace import _mask_url_credentials
from tools import (
    DEFAULT_TOOL_REGISTRY,
    EXECUTE_BASH_TOOL_NAME,
    ToolExpectedArtifact,
    ToolRegistry,
    UnknownToolError,
    granted_permission_from_constraints,
)
from workers.base import ArtifactReference, Worker, WorkerRequest, WorkerResult
from workers.cli_runtime import (
    CliRuntimeAdapter,
    CliRuntimeExecutionResult,
    CliRuntimeSettings,
    ShellSessionProtocol,
    collect_changed_files,
    run_cli_runtime_loop,
    settings_from_budget,
)
from workers.prompt import build_system_prompt

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ROOT_ENV_VAR = "CODE_AGENT_WORKSPACE_ROOT"


class ShellSessionFactory(Protocol):
    """Factory for opening a persistent shell session in a running container."""

    def __call__(self, container: DockerSandboxContainer) -> ShellSessionProtocol:
        """Return a ready-to-use shell session."""


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for sandbox bookkeeping."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _default_workspace_root() -> Path:
    """Return the default workspace root, honoring an environment override."""
    configured_root = os.environ.get(DEFAULT_WORKSPACE_ROOT_ENV_VAR)
    if configured_root:
        return Path(configured_root).expanduser()

    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        workspace_owner = f"uid-{getuid()}"
    else:
        username = os.environ.get("USER") or os.environ.get("USERNAME")
        if username:
            workspace_owner = f"user-{_slugify(username)}"
        else:
            workspace_owner = f"pid-{os.getpid()}"
    return Path(tempfile.gettempdir()) / f"code-agent-workspaces-{workspace_owner}"


def _workspace_task_id(request: WorkerRequest) -> str:
    """Build a readable workspace task identifier from the worker request."""
    source = request.session_id or request.task_text
    return f"codex-cli-{_slugify(source)}"


def _workspace_artifacts(workspace: WorkspaceHandle) -> list[ArtifactReference]:
    """Build the default artifact references for a retained workspace."""
    return [
        ArtifactReference(
            name="workspace",
            uri=str(workspace.workspace_path),
            artifact_type="workspace",
        )
    ]


def _apply_cleanup_outcome(result: WorkerResult, *, workspace_deleted: bool) -> WorkerResult:
    """Keep the reported result aligned with the final workspace state."""
    if not workspace_deleted:
        return result

    summary = (
        f"{result.summary.rstrip('.')} Workspace cleaned up per policy."
        if result.summary
        else "CodexCliWorker cleaned up the workspace per policy."
    )
    return result.model_copy(
        update={
            "summary": summary,
            "artifacts": [],
            "next_action_hint": None,
        }
    )


def _next_action_hint(execution: CliRuntimeExecutionResult) -> str:
    """Return the best follow-up hint for a retained workspace."""
    if execution.stop_reason == "permission_required":
        return "request_higher_permission"
    if execution.stop_reason in {"max_iterations", "worker_timeout", "budget_exceeded"}:
        return "increase_budget_or_reduce_scope"
    if execution.stop_reason == "adapter_error":
        return "inspect_worker_configuration"
    return "inspect_workspace_artifacts"


def _worker_result_from_execution(
    workspace: WorkspaceHandle,
    execution: CliRuntimeExecutionResult,
    *,
    files_changed: list[str],
) -> WorkerResult:
    """Map the shared CLI runtime output into the worker contract."""
    return WorkerResult(
        status=execution.status,
        summary=execution.summary,
        commands_run=execution.commands_run,
        files_changed=files_changed,
        artifacts=_workspace_artifacts(workspace),
        next_action_hint=_next_action_hint(execution),
    )


def _workspace_error_result(
    *,
    request: WorkerRequest,
    workspace: WorkspaceHandle,
    workspace_task_id: str,
    exc: Exception,
    summary_prefix: str,
    next_action_hint: str,
) -> WorkerResult:
    """Log a workspace-scoped failure and map it into the worker contract."""
    logger.exception(
        "Codex CLI worker failed inside a provisioned workspace",
        extra={
            "session_id": request.session_id,
            "workspace_id": workspace.workspace_id,
            "workspace_task_id": workspace_task_id,
        },
    )
    return WorkerResult(
        status="error",
        summary=f"{summary_prefix}: {exc}",
        artifacts=_workspace_artifacts(workspace),
        next_action_hint=next_action_hint,
    )


class CodexCliWorker(Worker):
    """Execute a bounded multi-turn CLI runtime inside a persistent sandbox."""

    def __init__(
        self,
        *,
        runtime_adapter: CliRuntimeAdapter,
        workspace_manager: WorkspaceManager | None = None,
        container_manager: DockerSandboxContainerManager | None = None,
        session_factory: ShellSessionFactory | None = None,
        workspace_root: str | Path | None = None,
        cleanup_policy: WorkspaceCleanupPolicy | None = None,
        runtime_settings: CliRuntimeSettings | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.runtime_adapter = runtime_adapter
        self.tool_registry = tool_registry or DEFAULT_TOOL_REGISTRY
        self.cleanup_policy = cleanup_policy or WorkspaceCleanupPolicy(
            delete_on_success=False,
            retain_on_failure=True,
        )
        self.workspace_manager = workspace_manager or WorkspaceManager(
            workspace_root or _default_workspace_root(),
            cleanup_policy=self.cleanup_policy,
        )
        self.container_manager = container_manager or DockerSandboxContainerManager()
        self._session_factory = session_factory or (lambda container: DockerShellSession(container))
        self.runtime_settings = runtime_settings or CliRuntimeSettings()

    async def run(self, request: WorkerRequest) -> WorkerResult:
        """Provision a workspace, run the CLI loop, and return a typed result."""
        return await asyncio.to_thread(self._run_sync, request)

    def _cleanup_workspace(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        *,
        workspace_task_id: str,
        run_succeeded: bool,
    ) -> bool:
        """Apply the workspace cleanup policy and swallow cleanup errors."""
        try:
            return self.workspace_manager.cleanup_workspace(
                workspace,
                succeeded=run_succeeded,
            )
        except WorkspaceManagerError:
            logger.exception(
                "Codex CLI worker failed to clean up workspace",
                extra={
                    "session_id": request.session_id,
                    "workspace_id": workspace.workspace_id,
                    "workspace_task_id": workspace_task_id,
                    "run_succeeded": run_succeeded,
                },
            )
            return False

    def _stop_container(self, container: DockerSandboxContainer | None) -> None:
        """Stop a persistent container while preserving the worker result."""
        if container is None:
            return
        try:
            self.container_manager.stop(container)
        except DockerSandboxContainerError:
            logger.exception(
                "Codex CLI worker failed to stop the persistent container",
                extra={
                    "workspace_id": container.workspace.workspace_id,
                    "task_id": container.workspace.task_id,
                    "container_name": container.container_name,
                },
            )

    def _close_session(self, session: ShellSessionProtocol | None) -> None:
        """Close the shell session while preserving the worker result."""
        if session is None:
            return
        try:
            session.close()
        except OSError:
            logger.exception("Codex CLI worker failed to close the persistent shell session")

    def _run_sync(self, request: WorkerRequest) -> WorkerResult:
        """Provision a workspace, run the CLI runtime, and return a typed result."""
        if request.repo_url is None or not request.repo_url.strip():
            return WorkerResult(
                status="error",
                summary=(
                    "CodexCliWorker requires a non-empty repo_url "
                    "to provision a sandbox workspace."
                ),
                next_action_hint="provide_repo_url",
            )

        workspace_task_id = _workspace_task_id(request)
        logger.info(
            "Starting Codex CLI worker run",
            extra={
                "session_id": request.session_id,
                "repo_url": _mask_url_credentials(request.repo_url),
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
        except (WorkspaceManagerError, OSError) as exc:
            logger.exception(
                "Codex CLI worker failed to provision workspace",
                extra={"session_id": request.session_id, "workspace_task_id": workspace_task_id},
            )
            return WorkerResult(
                status="error",
                summary=f"CodexCliWorker failed to provision a workspace: {exc}",
                next_action_hint="inspect_worker_configuration",
            )

        result: WorkerResult | None = None
        run_succeeded = False
        container: DockerSandboxContainer | None = None
        session: ShellSessionProtocol | None = None

        try:
            container = self.container_manager.start(
                DockerSandboxContainerRequest(workspace=workspace)
            )
            session = self._session_factory(container)
            runtime_settings = settings_from_budget(
                request.budget,
                defaults=self.runtime_settings,
            )
            granted_permission = granted_permission_from_constraints(request.constraints)
            bash_tool = self.tool_registry.require_tool(EXECUTE_BASH_TOOL_NAME)
            system_prompt = build_system_prompt(
                request,
                workspace.repo_path,
                tool_registry=self.tool_registry,
            )
            execution = run_cli_runtime_loop(
                self.runtime_adapter,
                session,
                system_prompt=system_prompt,
                settings=runtime_settings,
                tool_registry=self.tool_registry,
                granted_permission=granted_permission,
            )
            files_changed: list[str] = []
            if ToolExpectedArtifact.CHANGED_FILES in bash_tool.expected_artifacts:
                files_changed = collect_changed_files(
                    session,
                    timeout_seconds=runtime_settings.command_timeout_seconds,
                )
            result = _worker_result_from_execution(
                workspace,
                execution,
                files_changed=files_changed,
            )
            run_succeeded = result.status == "success"
        except (
            DockerSandboxContainerError,
            DockerShellSessionError,
            OSError,
            UnknownToolError,
        ) as exc:
            result = _workspace_error_result(
                request=request,
                workspace=workspace,
                workspace_task_id=workspace_task_id,
                exc=exc,
                summary_prefix="CodexCliWorker runtime setup failed",
                next_action_hint="inspect_worker_configuration",
            )
        finally:
            self._close_session(session)
            self._stop_container(container)
            workspace_deleted = self._cleanup_workspace(
                request,
                workspace,
                workspace_task_id=workspace_task_id,
                run_succeeded=run_succeeded,
            )

        if result is None:
            raise RuntimeError("Codex CLI worker execution completed without a result.")

        return _apply_cleanup_outcome(result, workspace_deleted=workspace_deleted)
