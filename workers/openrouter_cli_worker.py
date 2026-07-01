"""OpenRouter CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

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
    ToolRegistry,
    UnknownToolError,
)
from workers.async_runner import run_sync_with_cancellable_executor
from workers.base import Worker, WorkerRequest, WorkerResult
from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeSettings, ShellSessionProtocol
from workers.openrouter_cli_worker_utils import (
    _apply_cleanup_outcome,
    _workspace_error_result,
    _workspace_task_id,
)
from workers.runtime_executor import RuntimeExecutor
from workers.sandbox_adapter import SandboxSessionAdapter, ShellSessionFactory

logger = logging.getLogger(__name__)


class OpenRouterCliWorker(Worker):
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
            workspace_root or default_workspace_root(),
            cleanup_policy=self.cleanup_policy,
        )
        self.container_manager = container_manager or DockerSandboxContainerManager()
        self._session_factory = session_factory or (
            lambda container, secrets=None: DockerShellSession(container, secrets=secrets)
        )
        self.runtime_settings = runtime_settings or CliRuntimeSettings()

        self.sandbox_adapter = SandboxSessionAdapter(
            container_manager=self.container_manager,
            session_factory=self._session_factory,
        )
        self.runtime_executor = RuntimeExecutor(
            runtime_adapter=self.runtime_adapter,
            tool_registry=self.tool_registry,
            sandbox_adapter=self.sandbox_adapter,
            runtime_settings=self.runtime_settings,
        )

    async def run(
        self, request: WorkerRequest, *, system_prompt: str | None = None
    ) -> WorkerResult:
        """Provision a workspace, run the CLI loop, and return a typed result."""

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
                "OpenRouter CLI worker failed to clean up workspace",
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
                "OpenRouter CLI worker failed to stop the persistent container",
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
            logger.exception("OpenRouter CLI worker failed to close the persistent shell session")

    def _validate_request(self, request: WorkerRequest) -> WorkerResult | None:
        """Return an early error when required request inputs are missing."""
        if request.repo_url is None or not request.repo_url.strip():
            return WorkerResult(
                status="error",
                summary=(
                    "OpenRouterCliWorker requires a non-empty repo_url "
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
    ) -> WorkspaceHandle:
        """Create the sandbox workspace for this run."""
        return self.workspace_manager.create_workspace(
            WorkspaceRequest(
                task_id=workspace_task_id,
                repo_url=repo_url,
                branch=branch,
                cleanup_policy=self.cleanup_policy,
            )
        )

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
            )
        except (WorkspaceManagerError, OSError) as exc:
            logger.exception(
                "OpenRouter CLI worker failed to provision workspace",
                extra={"session_id": request.session_id, "workspace_task_id": workspace_task_id},
            )
            return WorkerResult(
                status="error",
                summary=f"OpenRouterCliWorker failed to provision a workspace: {exc}",
                failure_kind="sandbox_infra",
                next_action_hint="inspect_worker_configuration",
            )

    def _execute_runtime_loop(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        system_prompt_override: str | None,
        cancel_token: Callable[[], bool] | None,
    ) -> tuple[WorkerResult, DockerSandboxContainer | None, ShellSessionProtocol | None]:
        result = self.runtime_executor.execute(
            request,
            workspace=workspace,
            system_prompt_override=system_prompt_override,
            cancel_token=cancel_token,
        )
        return result, None, None

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
            raise RuntimeError(
                "OpenRouterCliWorker request validation expected repo_url to be set."
            )

        workspace_task_id = _workspace_task_id(request)
        logger.info(
            "Starting OpenRouter CLI worker run",
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
            result, container, session = self._execute_runtime_loop(
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
                summary_prefix="OpenRouterCliWorker runtime setup failed",
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
            raise RuntimeError("OpenRouter CLI worker execution completed without a result.")

        return _apply_cleanup_outcome(result, workspace_deleted=workspace_deleted)
