"""Codex CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

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
    CliRuntimeSettings,
    ShellSessionProtocol,
    settings_from_budget,
)

logger = logging.getLogger(__name__)

DEFAULT_CODEX_NATIVE_SANDBOX_MODE = "workspace-write"


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for sandbox bookkeeping."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _workspace_task_id(request: WorkerRequest) -> str:
    """Build a readable workspace task identifier from the worker request."""
    if request.task_id:
        return request.task_id
    source = request.session_id or request.task_text
    return f"codex-cli-{_slugify(source)}"


def _workspace_artifacts(workspace: WorkspaceHandle) -> list[ArtifactReference]:
    """Build the default artifact references for a retained workspace."""
    return [
        ArtifactReference(
            name="workspace",
            uri=workspace.workspace_path.as_uri(),
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
        failure_kind="sandbox_infra",
        artifacts=_workspace_artifacts(workspace),
        next_action_hint=next_action_hint,
    )


from workers.codex_cli_worker_native import CodexCliWorkerNativeMixin  # noqa: E402
from workers.runtime_executor import RuntimeExecutor  # noqa: E402
from workers.sandbox_adapter import SandboxSessionAdapter, ShellSessionFactory  # noqa: E402


class CodexCliWorker(CodexCliWorkerNativeMixin, Worker):
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
        native_sandbox_mode: str = DEFAULT_CODEX_NATIVE_SANDBOX_MODE,
        native_event_capture_enabled: bool = False,
        trusted_repo_patterns: list[str] | None = None,
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
        self.native_sandbox_mode = native_sandbox_mode.strip() or DEFAULT_CODEX_NATIVE_SANDBOX_MODE
        self.native_event_capture_enabled = native_event_capture_enabled
        self.trusted_repo_patterns: list[re.Pattern[str]] = []
        if trusted_repo_patterns:
            for pattern in trusted_repo_patterns:
                if not pattern or not pattern.strip():
                    continue
                try:
                    self.trusted_repo_patterns.append(re.compile(pattern))
                except re.error as exc:
                    logger.warning("Ignoring malformed trusted repository pattern: %s", exc)

        self.sandbox_adapter = SandboxSessionAdapter(
            container_manager=self.container_manager,
            session_factory=self._session_factory,  # type: ignore[arg-type]
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

        with start_optional_span(
            tracer_name="workers.codex_cli_worker",
            span_name="CodexCliWorker.run",
            attributes=with_span_kind(SPAN_KIND_AGENT),
            task_id=request.task_id,
            session_id=request.session_id,
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

    def _validate_request(self, request: WorkerRequest) -> WorkerResult | None:
        """Return an early error when required request inputs are missing."""
        if request.repo_url is None or not request.repo_url.strip():
            return WorkerResult(
                status="error",
                summary=(
                    "CodexCliWorker requires a non-empty repo_url to provision a sandbox workspace."
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
        """Create the sandbox workspace for this run."""
        if workspace_id:
            try:
                return self.workspace_manager.get_workspace(
                    workspace_id,
                    repo_url=repo_url,
                    branch=branch,
                    task_id=workspace_task_id,
                )
            except WorkspaceManagerError as exc:
                logger.warning(
                    f"CodexCliWorker failed to retrieve workspace {workspace_id}: {exc}. "
                    "Falling back to provisioning a new workspace."
                )
        return self.workspace_manager.create_workspace(
            WorkspaceRequest(
                task_id=workspace_task_id,
                repo_url=repo_url,
                branch=branch,
                cleanup_policy=self.cleanup_policy,
            )
        )

    def _execute_runtime_mode(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        system_prompt_override: str | None,
        cancel_token: Callable[[], bool] | None,
    ) -> tuple[WorkerResult, DockerSandboxContainer | None, ShellSessionProtocol | None]:
        runtime_mode = self._resolve_runtime_mode(request)
        if runtime_mode == WorkerRuntimeMode.TOOL_LOOP:
            logger.warning(
                "Codex tool_loop runtime mode is deprecated. "
                "Prefer native_agent defaults; keep tool_loop only for explicit legacy opt-in.",
                extra={
                    "session_id": request.session_id,
                    "worker_profile": request.worker_profile,
                    "runtime_mode": runtime_mode.value,
                },
            )
            result = self.runtime_executor.execute(
                request,
                workspace=workspace,
                system_prompt_override=system_prompt_override,
                cancel_token=cancel_token,
            )
            return result, None, None
        elif runtime_mode == WorkerRuntimeMode.NATIVE_AGENT:
            runtime_settings = settings_from_budget(
                request.budget,
                defaults=self.runtime_settings,
                task_id=request.task_id,
                session_id=request.session_id,
                read_only=request.read_only or bool(request.constraints.get("read_only")),
            )
            result = self._execute_native_runtime(
                request,
                workspace=workspace,
                runtime_settings=runtime_settings,
                runtime_mode=runtime_mode,
                system_prompt_override=system_prompt_override,
                cancel_token=cancel_token,
            )
            return result, None, None
        else:
            return self._runtime_mode_not_supported_result(runtime_mode), None, None

    def _provision_workspace_safe(
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
                "Codex CLI worker failed to provision workspace",
                extra={"session_id": request.session_id, "workspace_task_id": workspace_task_id},
            )
            return WorkerResult(
                status="error",
                summary=f"CodexCliWorker failed to provision a workspace: {exc}",
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
            raise RuntimeError("CodexCliWorker request validation expected repo_url to be set.")

        workspace_task_id = _workspace_task_id(request)
        logger.info(
            "Starting Codex CLI worker run",
            extra={
                "session_id": request.session_id,
                "repo_url": _mask_url_credentials(repo_url),
                "branch": request.branch,
                "workspace_task_id": workspace_task_id,
            },
        )

        workspace = self._provision_workspace_safe(request, repo_url, workspace_task_id)
        if isinstance(workspace, WorkerResult):
            return workspace

        result: WorkerResult | None = None
        run_succeeded = False
        container: DockerSandboxContainer | None = None
        session: ShellSessionProtocol | None = None

        try:
            result, container, session = self._execute_runtime_mode(
                request=request,
                workspace=workspace,
                system_prompt_override=system_prompt_override,
                cancel_token=cancel_token,
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
