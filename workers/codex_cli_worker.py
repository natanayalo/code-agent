"""Codex CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from apps.observability import (
    SPAN_KIND_AGENT,
    STATUS_ERROR,
    record_span_exception,
    set_span_status,
    set_span_status_from_outcome,
    start_optional_span,
    with_span_kind,
)
from db.enums import WorkerRuntimeMode
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
from sandbox.policy import is_in_container
from sandbox.redact import SecretRedactor
from sandbox.workspace import _mask_url_credentials, default_workspace_root
from tools import (
    DEFAULT_TOOL_REGISTRY,
    EXECUTE_BASH_TOOL_NAME,
    ToolExpectedArtifact,
    ToolPermissionLevel,
    ToolRegistry,
    UnknownToolError,
    granted_permission_from_constraints,
)
from workers.adapter_utils import format_native_run_summary
from workers.async_runner import run_sync_with_cancellable_executor
from workers.base import (
    ArtifactReference,
    FailureKind,
    Worker,
    WorkerCommand,
    WorkerRequest,
    WorkerResult,
)
from workers.cli_runtime import (
    CliRuntimeAdapter,
    CliRuntimeExecutionResult,
    CliRuntimeSettings,
    ShellSessionProtocol,
    run_cli_runtime_loop,
    settings_from_budget,
)
from workers.failure_taxonomy import classify_failure_kind
from workers.native_agent_models import NativeAgentRunResult
from workers.native_agent_runner import (
    NativeAgentRunRequest,
    run_native_agent,
)
from workers.post_run_lint import (
    collect_changed_files_and_apply_post_run_lint_format,
)
from workers.prompt import build_system_prompt
from workers.review import ReviewResult
from workers.self_review import (
    collect_diff_for_review,
    run_shared_self_review_fix_loop,
)

logger = logging.getLogger(__name__)

DEFAULT_CODEX_NATIVE_SANDBOX_MODE = "workspace-write"


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


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for sandbox bookkeeping."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _workspace_task_id(request: WorkerRequest) -> str:
    """Build a readable workspace task identifier from the worker request."""
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


def _next_action_hint(execution: CliRuntimeExecutionResult) -> str:
    """Return the best follow-up hint for a retained workspace."""
    if execution.stop_reason == "permission_required":
        return "request_higher_permission"
    if execution.stop_reason in {
        "max_iterations",
        "worker_timeout",
        "budget_exceeded",
        "stalled_in_inspection",
        "exploration_exhausted",
        "no_progress_before_budget",
    }:
        return "increase_budget_or_reduce_scope"
    if execution.stop_reason == "context_window":
        return "reduce_context_or_scope"
    if execution.stop_reason == "adapter_error":
        return "inspect_worker_configuration"
    return "inspect_workspace_artifacts"


def _worker_result_from_execution(
    workspace: WorkspaceHandle,
    execution: CliRuntimeExecutionResult,
    *,
    files_changed: list[str],
    post_run_lint_format: dict[str, object] | None = None,
    review_result: ReviewResult | None = None,
    diff_text: str | None = None,
    artifacts: list[ArtifactReference] | None = None,
) -> WorkerResult:
    """Map the shared CLI runtime output into the worker contract."""
    requested_permission = (
        execution.permission_decision.required_permission.value
        if execution.permission_decision is not None
        else None
    )
    budget_usage = execution.budget_ledger.model_dump(mode="json")
    if post_run_lint_format is not None:
        budget_usage["post_run_lint_format"] = post_run_lint_format
    return WorkerResult(
        status=execution.status,
        summary=execution.summary,
        failure_kind=classify_failure_kind(
            status=execution.status,
            stop_reason=execution.stop_reason,
            summary=execution.summary,
            commands_run=execution.commands_run,
        ),
        requested_permission=requested_permission,
        budget_usage=budget_usage,
        commands_run=execution.commands_run,
        files_changed=files_changed,
        artifacts=[*_workspace_artifacts(workspace), *(artifacts or [])],
        review_result=review_result,
        diff_text=diff_text,
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
        failure_kind="sandbox_infra",
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

    async def run(
        self, request: WorkerRequest, *, system_prompt: str | None = None
    ) -> WorkerResult:
        """Provision a workspace, run the CLI loop, and return a typed result."""

        with start_optional_span(
            tracer_name="workers.codex_cli_worker",
            span_name="CodexCliWorker.run",
            attributes=with_span_kind(SPAN_KIND_AGENT),
        ):

            def _run_sync(cancel_requested: Callable[[], bool]) -> WorkerResult:
                return self._run_sync(
                    request,
                    cancel_token=cancel_requested,
                    system_prompt_override=system_prompt,
                )

            try:
                result = await run_sync_with_cancellable_executor(_run_sync)
                set_span_status_from_outcome(result.status, result.summary)
                return result
            except Exception as exc:
                record_span_exception(exc)
                set_span_status(STATUS_ERROR, str(exc))
                raise

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

    def _setup_runtime_phase(
        self,
        request: WorkerRequest,
        *,
        workspace: WorkspaceHandle,
        system_prompt_override: str | None,
    ) -> _RuntimeSetup:
        """Prepare runtime dependencies before entering the main CLI loop."""
        tool_names = request.tools
        if tool_names is None:
            tool_names = [tool.name for tool in self.tool_registry.list_tools()]

        scoped_secrets = self.tool_registry.get_scoped_secrets(
            tool_names=tool_names,
            available_secrets=request.secrets,
        )

        container = self.container_manager.start(
            DockerSandboxContainerRequest(
                workspace=workspace,
                environment=scoped_secrets,
            )
        )
        session: ShellSessionProtocol | None = None
        try:
            session = self._session_factory(container, secrets=request.secrets)
            runtime_settings = settings_from_budget(
                request.budget,
                defaults=self.runtime_settings,
            )
            granted_permission = granted_permission_from_constraints(request.constraints)
            bash_tool = self.tool_registry.require_tool(EXECUTE_BASH_TOOL_NAME)
            fallback_command_template = (
                request.constraints.get("post_run_lint_format_command")
                if isinstance(request.constraints.get("post_run_lint_format_command"), str)
                else None
            )
            system_prompt = (
                system_prompt_override
                if system_prompt_override is not None
                else build_system_prompt(
                    request,
                    workspace.repo_path,
                    tool_registry=self.tool_registry,
                )
            )

            return _RuntimeSetup(
                container=container,
                session=session,
                runtime_settings=runtime_settings,
                granted_permission=granted_permission,
                expects_changed_files=(
                    ToolExpectedArtifact.CHANGED_FILES in bash_tool.expected_artifacts
                ),
                fallback_command_template=fallback_command_template,
                system_prompt=system_prompt,
            )
        except Exception:
            if session is not None:
                self._close_session(session)
            self._stop_container(container)
            raise

    def _execute_runtime_phase(
        self,
        request: WorkerRequest,
        *,
        workspace: WorkspaceHandle,
        runtime_setup: _RuntimeSetup,
        cancel_token: Callable[[], bool] | None,
    ) -> _RuntimeExecutionPhase:
        """Execute the main CLI loop and post-run review/lint phases."""
        execution = run_cli_runtime_loop(
            self.runtime_adapter,
            runtime_setup.session,
            system_prompt=runtime_setup.system_prompt,
            settings=runtime_setup.runtime_settings,
            tool_registry=self.tool_registry,
            granted_permission=runtime_setup.granted_permission,
            working_directory=workspace.repo_path,
            cancel_token=cancel_token,
            model_name=getattr(self.runtime_adapter, "model", None),
        )

        files_changed, lint_format_result, lint_format_artifacts = (
            collect_changed_files_and_apply_post_run_lint_format(
                session=runtime_setup.session,
                execution=execution,
                expect_changed_files_artifact=runtime_setup.expects_changed_files,
                repo_path_for_detection=workspace.repo_path,
                repo_working_directory=Path(runtime_setup.container.working_dir),
                timeout_seconds=runtime_setup.runtime_settings.command_timeout_seconds,
                fallback_command_template=runtime_setup.fallback_command_template,
            )
        )

        review_result: ReviewResult | None = None
        if execution.status == "success":
            (
                review_result,
                files_changed,
                lint_format_result,
                lint_format_artifacts,
            ) = run_shared_self_review_fix_loop(
                execution=execution,
                task_text=request.task_text,
                constraints=request.constraints,
                runtime_adapter=self.runtime_adapter,
                runtime_settings=runtime_setup.runtime_settings,
                system_prompt=runtime_setup.system_prompt,
                repo_path=workspace.repo_path,
                files_changed=files_changed,
                lint_format_result=lint_format_result,
                lint_format_artifacts=lint_format_artifacts,
                post_run_lint_collector=(
                    lambda current_execution, existing_files: (
                        collect_changed_files_and_apply_post_run_lint_format(
                            session=runtime_setup.session,
                            execution=current_execution,
                            expect_changed_files_artifact=runtime_setup.expects_changed_files,
                            repo_path_for_detection=workspace.repo_path,
                            repo_working_directory=Path(runtime_setup.container.working_dir),
                            existing_files_changed=existing_files,
                            timeout_seconds=runtime_setup.runtime_settings.command_timeout_seconds,
                            fallback_command_template=runtime_setup.fallback_command_template,
                        )
                    )
                ),
                tool_registry=self.tool_registry,
                granted_permission=runtime_setup.granted_permission,
                session=runtime_setup.session,
                cancel_token=cancel_token,
                model_name=getattr(self.runtime_adapter, "model", None),
                adapter_failure_log_message=(
                    "Codex CLI worker self-review adapter failed; recording explicit "
                    "no-findings fallback."
                ),
                adapter_failure_logger=logger,
                check_cancel_before_review=True,
            )

        return _RuntimeExecutionPhase(
            execution=execution,
            files_changed=files_changed,
            lint_format_result=lint_format_result,
            lint_format_artifacts=lint_format_artifacts,
            review_result=review_result,
        )

    def _finalize_runtime_result(
        self,
        workspace: WorkspaceHandle,
        runtime_phase: _RuntimeExecutionPhase,
        *,
        runtime_settings: CliRuntimeSettings,
        cancel_token: Callable[[], bool] | None,
    ) -> WorkerResult:
        """Map runtime phase outputs to the worker result contract."""
        result = _worker_result_from_execution(
            workspace,
            runtime_phase.execution,
            files_changed=runtime_phase.files_changed,
            post_run_lint_format=runtime_phase.lint_format_result,
            review_result=runtime_phase.review_result,
            diff_text=collect_diff_for_review(
                workspace.repo_path,
                timeout_seconds=runtime_settings.command_timeout_seconds,
            )
            if runtime_phase.execution.status == "success"
            else None,
            artifacts=runtime_phase.lint_format_artifacts,
        )
        if cancel_token and cancel_token():
            result.status = "error"
            result.summary = "CLI runtime loop was cancelled by the orchestrator timeout."
            result.failure_kind = "timeout"
            result.next_action_hint = "inspect_workspace_artifacts"
        return result

    def _resolve_runtime_mode(self, request: WorkerRequest) -> WorkerRuntimeMode:
        """Resolve effective runtime mode from request override or worker defaults."""
        if request.runtime_mode is not None:
            return request.runtime_mode
        return self.default_runtime_mode

    def _runtime_mode_not_supported_result(self, runtime_mode: WorkerRuntimeMode) -> WorkerResult:
        """Return a structured failure for unsupported execution runtime modes."""
        return WorkerResult(
            status="failure",
            summary=(
                "CodexCliWorker does not support runtime mode "
                f"`{runtime_mode.value}`. Supported modes: native_agent, tool_loop."
            ),
            failure_kind="provider_error",
            next_action_hint="inspect_worker_configuration",
        )

    def _build_native_command(
        self,
        *,
        workspace: WorkspaceHandle,
        request: WorkerRequest,
        final_message_path: Path,
        runtime_mode: WorkerRuntimeMode,
    ) -> tuple[list[str], dict[str, Any]]:
        """Build a one-shot `codex exec` command for native-agent mode."""
        executable = getattr(self.runtime_adapter, "executable", "codex")
        model = getattr(self.runtime_adapter, "model", None)
        profile = getattr(self.runtime_adapter, "profile", None)
        read_only_requested = bool(request.constraints.get("read_only"))

        # Sandbox selection policy (T-172)
        in_container = is_in_container()
        repo_trusted = False
        repo_url = request.repo_url or ""
        for pattern in self.trusted_repo_patterns:
            if pattern.search(repo_url):
                repo_trusted = True
                break

        if read_only_requested:
            sandbox_mode = "read-only"
        elif in_container and repo_trusted:
            sandbox_mode = "danger-full-access"
        else:
            sandbox_mode = self.native_sandbox_mode or DEFAULT_CODEX_NATIVE_SANDBOX_MODE

        logger.info(
            "Selected Codex native sandbox mode: %s",
            sandbox_mode,
            extra={
                "session_id": request.session_id,
                "in_container": bool(in_container),
                "repo_trusted": bool(repo_trusted),
                "read_only_requested": bool(read_only_requested),
            },
        )

        sandbox_metadata = {
            "sandbox_mode": sandbox_mode,
            "in_container": in_container,
            "repo_trusted": repo_trusted,
            "read_only_requested": read_only_requested,
        }

        command = [
            executable,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            sandbox_mode,
            "--color",
            "never",
            "--output-last-message",
            str(final_message_path),
            "--ephemeral",
            "-C",
            str(workspace.repo_path),
        ]
        if model:
            command.extend(["--model", str(model)])
        if profile:
            command.extend(["--profile", str(profile)])
        if self.native_event_capture_enabled and runtime_mode == WorkerRuntimeMode.NATIVE_AGENT:
            command.append("--json")
        command.append("-")
        return command, sandbox_metadata

    def _build_native_prompt(self, *, system_prompt: str, request: WorkerRequest) -> str:
        """Build the native-agent prompt packet for one-shot Codex execution."""
        task_text = request.task_text.strip()
        sections = [
            system_prompt.strip(),
            "## Native Execution Task",
            task_text,
            "## Output",
            (
                "Return a concise final summary of what changed, verification performed, "
                "and any remaining blocker."
            ),
        ]
        return "\n\n".join(section for section in sections if section.strip())

    def _native_next_action_hint(self, native_result: NativeAgentRunResult) -> str:
        """Map native run outcomes to follow-up hints."""
        if native_result.timed_out:
            return "increase_budget_or_reduce_scope"
        if native_result.status == "error":
            return "inspect_worker_configuration"
        return "inspect_workspace_artifacts"

    def _native_failure_kind(self, native_result: NativeAgentRunResult) -> FailureKind | None:
        """Classify failure kind for native-agent outcomes."""
        if native_result.status == "success":
            return None
        if native_result.timed_out:
            return "timeout"
        return classify_failure_kind(
            status=native_result.status,
            summary=format_native_run_summary(native_result),
            commands_run=[
                WorkerCommand(
                    command=native_result.command,
                    exit_code=native_result.exit_code,
                    duration_seconds=native_result.duration_seconds,
                )
            ],
        )

    def _execute_native_runtime(
        self,
        request: WorkerRequest,
        *,
        workspace: WorkspaceHandle,
        runtime_settings: CliRuntimeSettings,
        runtime_mode: WorkerRuntimeMode,
        system_prompt_override: str | None,
        cancel_token: Callable[[], bool] | None,
    ) -> WorkerResult:
        """Execute one native-agent `codex exec` run and map it into WorkerResult."""
        if cancel_token and cancel_token():
            return WorkerResult(
                status="error",
                summary="Codex native-agent run was cancelled before execution.",
                failure_kind="timeout",
                artifacts=_workspace_artifacts(workspace),
                next_action_hint="inspect_workspace_artifacts",
            )

        system_prompt = (
            system_prompt_override
            if system_prompt_override is not None
            else build_system_prompt(
                request,
                workspace.repo_path,
                tool_registry=self.tool_registry,
            )
        )
        final_message_path = workspace.workspace_path / ".code-agent" / "native-final-message.json"
        final_message_path.parent.mkdir(parents=True, exist_ok=True)
        events_path = (
            workspace.workspace_path / ".code-agent" / "native-events.jsonl"
            if self.native_event_capture_enabled
            else None
        )
        command, sandbox_metadata = self._build_native_command(
            workspace=workspace,
            request=request,
            final_message_path=final_message_path,
            runtime_mode=runtime_mode,
        )
        native_result = run_native_agent(
            NativeAgentRunRequest(
                command=command,
                prompt=self._build_native_prompt(system_prompt=system_prompt, request=request),
                repo_path=workspace.repo_path,
                workspace_path=workspace.workspace_path,
                timeout_seconds=runtime_settings.worker_timeout_seconds,
                diff_timeout_seconds=runtime_settings.command_timeout_seconds,
                changed_files_timeout_seconds=runtime_settings.command_timeout_seconds,
                env=getattr(self.runtime_adapter, "env", None),
                final_message_path=final_message_path,
                events_path=events_path,
                collect_diff=True,
                collect_changed_files=True,
                redactor=SecretRedactor(list((request.secrets or {}).values())),
            )
        )

        summary = format_native_run_summary(native_result)
        result = WorkerResult(
            status=native_result.status,
            summary=summary,
            failure_kind=self._native_failure_kind(native_result),
            budget_usage={
                "runtime_mode": runtime_mode.value,
                "native_agent": {
                    "duration_seconds": native_result.duration_seconds,
                    "exit_code": native_result.exit_code,
                    "timed_out": native_result.timed_out,
                    "event_capture_enabled": self.native_event_capture_enabled,
                    **sandbox_metadata,
                },
            },
            commands_run=[
                WorkerCommand(
                    command=native_result.command,
                    exit_code=native_result.exit_code,
                    duration_seconds=native_result.duration_seconds,
                )
            ],
            files_changed=native_result.files_changed,
            artifacts=[*_workspace_artifacts(workspace), *native_result.artifacts],
            diff_text=native_result.diff_text,
            next_action_hint=self._native_next_action_hint(native_result),
        )
        if cancel_token and cancel_token():
            result.status = "error"
            result.summary = "Codex native-agent run was cancelled by the orchestrator timeout."
            result.failure_kind = "timeout"
            result.next_action_hint = "inspect_workspace_artifacts"
        return result

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

        try:
            workspace = self._provision_workspace(
                repo_url,
                request.branch,
                workspace_task_id=workspace_task_id,
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

        result: WorkerResult | None = None
        run_succeeded = False
        container: DockerSandboxContainer | None = None
        session: ShellSessionProtocol | None = None

        try:
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
            if runtime_mode == WorkerRuntimeMode.NATIVE_AGENT:
                runtime_settings = settings_from_budget(
                    request.budget,
                    defaults=self.runtime_settings,
                )
                result = self._execute_native_runtime(
                    request,
                    workspace=workspace,
                    runtime_settings=runtime_settings,
                    runtime_mode=runtime_mode,
                    system_prompt_override=system_prompt_override,
                    cancel_token=cancel_token,
                )
                run_succeeded = result.status == "success"
            elif runtime_mode == WorkerRuntimeMode.TOOL_LOOP:
                runtime_setup = self._setup_runtime_phase(
                    request,
                    workspace=workspace,
                    system_prompt_override=system_prompt_override,
                )
                container = runtime_setup.container
                session = runtime_setup.session
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
                run_succeeded = result.status == "success"
            else:
                result = self._runtime_mode_not_supported_result(runtime_mode)
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
