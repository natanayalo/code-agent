"""Gemini CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from apps.observability import (
    SPAN_KIND_AGENT,
    start_optional_span,
    with_span_kind,
)
from db.enums import WorkerRuntimeMode
from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerError,
    DockerSandboxContainerManager,
    DockerShellSession,
    DockerShellSessionError,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceRequest,
)
from sandbox.workspace import _mask_url_credentials, default_workspace_root
from tools import (
    DEFAULT_TOOL_REGISTRY,
    ToolPermissionLevel,
    ToolRegistry,
    UnknownToolError,
)
from workers.async_runner import run_sync_with_cancellable_executor
from workers.base import (
    ArtifactReference,
    Worker,
    WorkerRequest,
    WorkerResult,
)
from workers.cli_runtime import (
    CliRuntimeAdapter,
    CliRuntimeExecutionResult,
    CliRuntimeSettings,
    ShellSessionProtocol,
    settings_from_budget,
)
from workers.gemini_cli_worker_native import (
    GeminiCliWorkerNativeMixin,
    _apply_cleanup_outcome,
    _prepare_workspace_gemini_home,
    _workspace_error_result,
    _workspace_task_id,
)
from workers.gemini_cli_worker_runtime import GeminiCliWorkerRuntimeMixin
from workers.review import ReviewResult

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_NATIVE_SANDBOX_ENABLED = True
DEFAULT_GEMINI_NATIVE_APPROVAL_MODE = "default"


@dataclass
class _RuntimeSetup:
    """Container/session/runtime context prepared before entering the CLI loop."""

    container: DockerSandboxContainer
    session: ShellSessionProtocol
    runtime_settings: CliRuntimeSettings
    granted_permission: ToolPermissionLevel
    expects_changed_files: bool
    fallback_command_template: str | None
    system_prompt: str


@dataclass
class _RuntimeExecutionPhase:
    """Outputs captured from runtime execution and post-processing phases."""

    execution: CliRuntimeExecutionResult
    files_changed: list[str]
    lint_format_result: dict[str, object] | None
    lint_format_artifacts: list[ArtifactReference]
    review_result: ReviewResult | None


class ShellSessionFactory(Protocol):
    """Factory for opening a persistent shell session in a running container."""

    def __call__(
        self,
        container: DockerSandboxContainer,
        *,
        secrets: dict[str, str] | None = None,
    ) -> ShellSessionProtocol:
        """Return a ready-to-use shell session."""


class GeminiCliWorker(GeminiCliWorkerRuntimeMixin, GeminiCliWorkerNativeMixin, Worker):
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
        default_runtime_mode: WorkerRuntimeMode = WorkerRuntimeMode.TOOL_LOOP,
        native_sandbox_enabled: bool = DEFAULT_GEMINI_NATIVE_SANDBOX_ENABLED,
    ) -> None:
        self.runtime_adapter = runtime_adapter
        self.tool_registry = tool_registry or DEFAULT_TOOL_REGISTRY
        self.cleanup_policy = cleanup_policy or WorkspaceCleanupPolicy(
            delete_on_success=False,
            retain_on_failure=True,
        )
        self.workspace_manager = workspace_manager or WorkspaceManager(
            workspace_root or default_workspace_root(),
            cleanup_policy=self.cleanup_policy,
        )
        self.container_manager = container_manager or DockerSandboxContainerManager()
        self._session_factory = session_factory or (
            lambda container, secrets=None: DockerShellSession(container, secrets=secrets)
        )
        self.runtime_settings = runtime_settings or CliRuntimeSettings()
        self.default_runtime_mode = default_runtime_mode
        self.native_sandbox_enabled = native_sandbox_enabled

    async def run(
        self, request: WorkerRequest, *, system_prompt: str | None = None
    ) -> WorkerResult:
        """Provision a workspace, run the CLI loop, and return a typed result."""

        with start_optional_span(
            tracer_name="workers.gemini_cli_worker",
            span_name="GeminiCliWorker.run",
            attributes=with_span_kind(SPAN_KIND_AGENT),
        ):

            def _run_sync(cancel_requested: Callable[[], bool]) -> WorkerResult:
                return self._run_sync(
                    request,
                    cancel_token=cancel_requested,
                    system_prompt_override=system_prompt,
                )

            return await run_sync_with_cancellable_executor(_run_sync)

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
                "Gemini CLI worker failed to clean up workspace",
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
                "Gemini CLI worker failed to stop the persistent container",
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
            logger.exception("Gemini CLI worker failed to close the persistent shell session")

    def _validate_request(self, request: WorkerRequest) -> WorkerResult | None:
        """Return an early error when required request inputs are missing."""
        if request.repo_url is None or not request.repo_url.strip():
            return WorkerResult(
                status="error",
                summary=(
                    "GeminiCliWorker requires a non-empty repo_url "
                    "to provision a sandbox workspace."
                ),
                failure_kind="unknown",
                next_action_hint="provide_repo_url",
            )
        return None

    def _provision_workspace(
        self,
        repo_url: str,
        branch: str | None,
        *,
        workspace_task_id: str,
        workspace_id: str | None = None,
    ) -> WorkspaceHandle:
        """Create or retrieve the sandbox workspace for this run."""
        if workspace_id:
            return self.workspace_manager.get_workspace(
                workspace_id,
                repo_url=repo_url,
                branch=branch,
                task_id=workspace_task_id,
            )
        return self.workspace_manager.create_workspace(
            WorkspaceRequest(
                task_id=workspace_task_id,
                repo_url=repo_url,
                branch=branch,
                cleanup_policy=self.cleanup_policy,
            )
        )

    def _execute_runtime_mode_switch(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        system_prompt_override: str | None,
        cancel_token: Callable[[], bool] | None,
    ) -> tuple[WorkerResult, DockerSandboxContainer | None, ShellSessionProtocol | None]:
        _prepare_workspace_gemini_home(workspace_path=workspace.workspace_path)
        runtime_mode = self._resolve_runtime_mode(request)
        container: DockerSandboxContainer | None = None
        session: ShellSessionProtocol | None = None
        result: WorkerResult | None = None

        if runtime_mode == WorkerRuntimeMode.TOOL_LOOP:
            logger.warning(
                "Gemini tool_loop runtime mode is deprecated. "
                "Prefer native_agent defaults; keep tool_loop only for explicit legacy opt-in.",
                extra={
                    "session_id": request.session_id,
                    "worker_profile": request.worker_profile,
                    "runtime_mode": runtime_mode.value,
                },
            )
        if runtime_mode in {
            WorkerRuntimeMode.NATIVE_AGENT,
            WorkerRuntimeMode.REVIEWER_ONLY,
            WorkerRuntimeMode.PLANNER_ONLY,
        }:
            runtime_settings = settings_from_budget(
                request.budget,
                defaults=self.runtime_settings,
                task_id=request.task_id,
                session_id=request.session_id,
            )
            result = self._execute_native_runtime(
                request,
                workspace=workspace,
                runtime_settings=runtime_settings,
                runtime_mode=runtime_mode,
                system_prompt_override=system_prompt_override,
                cancel_token=cancel_token,
            )
        elif runtime_mode == WorkerRuntimeMode.TOOL_LOOP:
            runtime_setup = self._setup_runtime_phase(
                request,
                workspace=workspace,
                system_prompt_override=system_prompt_override,
            )
            container = runtime_setup.container
            session = runtime_setup.session
            try:
                runtime_phase = self._execute_runtime_phase(
                    request,
                    workspace=workspace,
                    runtime_setup=runtime_setup,
                    cancel_token=cancel_token,
                )
                result = self._finalize_runtime_result(
                    workspace,
                    runtime_phase,
                    runtime_settings=runtime_setup.runtime_settings,
                    cancel_token=cancel_token,
                )
            except Exception as e:
                logger.debug(f"Exception in runtime loop: {e}", exc_info=True)
                self._close_session(session)
                self._stop_container(container)
                raise
        else:
            result = self._runtime_mode_not_supported_result(runtime_mode)

        if result is None:
            raise RuntimeError(
                "Gemini CLI worker runtime mode execution completed without a result."
            )

        return result, container, session

    def _safe_provision_workspace(
        self,
        request: WorkerRequest,
        repo_url: str,
        workspace_task_id: str,
    ) -> WorkspaceHandle | WorkerResult:
        try:
            return self._provision_workspace(
                repo_url,
                request.branch,
                workspace_task_id=workspace_task_id,
                workspace_id=request.workspace_id,
            )
        except (WorkspaceManagerError, OSError) as exc:
            logger.exception(
                "Gemini CLI worker failed to provision workspace",
                extra={"session_id": request.session_id, "workspace_task_id": workspace_task_id},
            )
            return WorkerResult(
                status="error",
                summary=f"GeminiCliWorker failed to provision a workspace: {exc}",
                failure_kind="sandbox_infra",
                next_action_hint="inspect_worker_configuration",
            )

    def _run_sync(
        self,
        request: WorkerRequest,
        cancel_token: Callable[[], bool] | None = None,
        system_prompt_override: str | None = None,
    ) -> WorkerResult:
        """Provision a workspace, run the CLI runtime, and return a typed result."""
        invalid_request_result = self._validate_request(request)
        if invalid_request_result is not None:
            return invalid_request_result
        repo_url = request.repo_url
        if repo_url is None:
            raise RuntimeError("GeminiCliWorker request validation expected repo_url to be set.")

        workspace_task_id = _workspace_task_id(request)
        logger.info(
            "Starting Gemini CLI worker run",
            extra={
                "session_id": request.session_id,
                "repo_url": _mask_url_credentials(repo_url),
                "branch": request.branch,
                "workspace_task_id": workspace_task_id,
            },
        )

        workspace_or_error = self._safe_provision_workspace(request, repo_url, workspace_task_id)
        if isinstance(workspace_or_error, WorkerResult):
            return workspace_or_error
        workspace = workspace_or_error

        result: WorkerResult | None = None
        run_succeeded = False
        container: DockerSandboxContainer | None = None
        session: ShellSessionProtocol | None = None

        try:
            result, container, session = self._execute_runtime_mode_switch(
                request,
                workspace=workspace,
                system_prompt_override=system_prompt_override,
                cancel_token=cancel_token,
            )
            run_succeeded = result.status == "success"
        except (
            DockerSandboxContainerError,
            DockerShellSessionError,
            OSError,
            RuntimeError,
            UnknownToolError,
        ) as exc:
            result = _workspace_error_result(
                request=request,
                workspace=workspace,
                workspace_task_id=workspace_task_id,
                exc=exc,
                summary_prefix="GeminiCliWorker runtime setup failed",
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
            raise RuntimeError("Gemini CLI worker execution completed without a result.")

        return _apply_cleanup_outcome(result, workspace_deleted=workspace_deleted)
