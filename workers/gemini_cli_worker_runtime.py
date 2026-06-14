"""Gemini CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from apps.observability import (
    set_span_status_from_outcome,
)
from db.enums import WorkerRuntimeMode
from sandbox import (
    DockerSandboxContainer,
    DockerSandboxContainerRequest,
    WorkspaceHandle,
)
from sandbox.redact import SecretRedactor
from tools import (
    EXECUTE_BASH_TOOL_NAME,
    ToolExpectedArtifact,
    ToolPermissionLevel,
    granted_permission_from_constraints,
)
from workers.base import (
    ArtifactReference,
    WorkerRequest,
    WorkerResult,
)
from workers.cli_adapter_utils import build_worker_result
from workers.cli_runtime import (
    CliRuntimeExecutionResult,
    CliRuntimeSettings,
    ShellSessionProtocol,
    run_cli_runtime_loop,
    settings_from_budget,
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


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for sandbox bookkeeping."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _workspace_task_id(request: WorkerRequest) -> str:
    """Build a readable workspace task identifier from the worker request."""
    if request.task_id:
        return request.task_id
    source = request.session_id or request.task_text
    return f"gemini-cli-{_slugify(source)}"


def _workspace_artifacts(workspace: WorkspaceHandle) -> list[ArtifactReference]:
    """Build the default artifact references for a retained workspace."""
    return [
        ArtifactReference(
            name="workspace",
            uri=workspace.workspace_path.as_uri(),
            artifact_type="workspace",
        )
    ]


def _prepare_workspace_gemini_home(
    *,
    workspace_path: Path,
    source_gemini_home: Path | None = None,
) -> None:
    """Best-effort sync of Gemini auth/config into isolated workspace HOME."""
    candidates: list[Path] = []
    if source_gemini_home is not None:
        candidates.append(source_gemini_home)
    env_gemini_home = os.environ.get("GEMINI_HOME")
    if env_gemini_home:
        candidates.append(Path(env_gemini_home))
    try:
        candidates.append(Path.home() / ".gemini")
    except Exception:
        pass
    candidates.append(Path("/root/.gemini"))

    resolved_source: Path | None = None
    for candidate in candidates:
        expanded = candidate.expanduser()
        try:
            if expanded.exists() and expanded.is_dir():
                resolved_source = expanded
                break
        except OSError:
            continue

    if resolved_source is None:
        return

    agent_home = workspace_path / ".agent_home"
    agent_home.mkdir(parents=True, exist_ok=True)
    target = agent_home / ".gemini"
    target_settings = target / "settings.json"
    if target.exists() or target.is_symlink():
        # Repair stale or partial workspace state: if the auth settings file is
        # missing, refresh the target from the resolved source.
        if target_settings.exists():
            try:
                if not target.is_symlink() or os.readlink(target) == str(resolved_source):
                    return
            except OSError:
                pass
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
        except OSError:
            logger.warning(
                "Failed to reset stale workspace Gemini home before sync",
                extra={"target": str(target)},
                exc_info=True,
            )
            return

    try:
        target.symlink_to(resolved_source, target_is_directory=True)
        return
    except OSError:
        logger.info(
            "Falling back to copy for workspace Gemini home sync",
            extra={"source": str(resolved_source), "target": str(target)},
        )

    try:
        shutil.copytree(resolved_source, target)
    except OSError:
        logger.warning(
            "Failed to sync Gemini home into workspace agent home",
            extra={"source": str(resolved_source), "target": str(target)},
            exc_info=True,
        )


def _apply_cleanup_outcome(result: WorkerResult, *, workspace_deleted: bool) -> WorkerResult:
    """Keep the reported result aligned with the final workspace state."""
    if not workspace_deleted:
        return result

    summary = (
        f"{result.summary.rstrip('.')} Workspace cleaned up per policy."
        if result.summary
        else "GeminiCliWorker cleaned up the workspace per policy."
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
    return build_worker_result(
        execution=execution,
        files_changed=files_changed,
        requested_permission=requested_permission,
        post_run_lint_format=post_run_lint_format,
        review_result=review_result,
        diff_text=diff_text,
        artifacts=[*_workspace_artifacts(workspace), *(artifacts or [])],
        next_action_hint=_next_action_hint(execution),
        workspace_id=workspace.workspace_id,
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
        "Gemini CLI worker failed inside a provisioned workspace",
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


class GeminiCliWorkerRuntimeMixin:
    """Extracted mixin methods for Gemini CLI worker."""

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
            tool_names = [tool.name for tool in self.tool_registry.list_tools()]  # type: ignore[attr-defined]

        scoped_secrets = self.tool_registry.get_scoped_secrets(  # type: ignore[attr-defined]
            tool_names=tool_names,
            available_secrets=request.secrets,
        )

        # Determine if network should be enabled (T-143 escalation policy)
        network_enabled = request.network_enabled
        if not network_enabled:
            granted_permission = granted_permission_from_constraints(request.constraints)
            if granted_permission == ToolPermissionLevel.NETWORKED_WRITE:
                for name in tool_names:
                    if (tool := self.tool_registry.get_tool(name)) and tool.network_required:  # type: ignore[attr-defined]
                        network_enabled = True
                        break

        container = self.container_manager.start(  # type: ignore[attr-defined]
            DockerSandboxContainerRequest(
                workspace=workspace,
                environment=scoped_secrets,
                network_enabled=network_enabled,
                read_only_workspace=request.read_only or bool(request.constraints.get("read_only")),
            )
        )
        session: ShellSessionProtocol | None = None
        try:
            session = self._session_factory(container, secrets=request.secrets)  # type: ignore[attr-defined]
            runtime_settings = settings_from_budget(
                request.budget,
                defaults=self.runtime_settings,  # type: ignore[attr-defined]
                task_id=request.task_id,
                session_id=request.session_id,
                read_only=request.read_only or bool(request.constraints.get("read_only")),
            )
            granted_permission = granted_permission_from_constraints(request.constraints)
            bash_tool = self.tool_registry.require_tool(EXECUTE_BASH_TOOL_NAME)  # type: ignore[attr-defined]
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
                    tool_registry=self.tool_registry,  # type: ignore[attr-defined]
                )
            )

            return _RuntimeSetup(
                container=container,
                session=session,  # type: ignore[arg-type]
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
                self._close_session(session)  # type: ignore[attr-defined]
            self._stop_container(container)  # type: ignore[attr-defined]
            raise

    def _run_self_review_if_successful(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        runtime_setup: _RuntimeSetup,
        execution: CliRuntimeExecutionResult,
        files_changed: list[str],
        lint_format_result: dict[str, Any] | None,
        lint_format_artifacts: list[ArtifactReference],
        cancel_token: Callable[[], bool] | None,
    ) -> tuple[ReviewResult | None, list[str], dict[str, Any], list[ArtifactReference]]:
        if execution.status != "success":
            return None, files_changed, lint_format_result or {}, lint_format_artifacts

        return run_shared_self_review_fix_loop(
            execution=execution,
            task_text=request.task_text,
            constraints=request.constraints,
            runtime_adapter=self.runtime_adapter,  # type: ignore[attr-defined]
            runtime_settings=runtime_setup.runtime_settings,
            system_prompt=runtime_setup.system_prompt,
            repo_path=workspace.repo_path,
            files_changed=files_changed,
            lint_format_result=lint_format_result or {},
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
            tool_registry=self.tool_registry,  # type: ignore[attr-defined]
            granted_permission=runtime_setup.granted_permission,
            session=runtime_setup.session,
            cancel_token=cancel_token,
            task_id=request.task_id,
            session_id=request.session_id,
            model_name=getattr(self.runtime_adapter, "model", None),  # type: ignore[attr-defined]
            adapter_failure_log_message=(
                "Gemini CLI worker self-review adapter failed; recording explicit "
                "no-findings fallback."
            ),
            adapter_failure_logger=logger,
            check_cancel_before_review=True,
        )

    def _execute_runtime_phase(
        self,
        request: WorkerRequest,
        *,
        workspace: WorkspaceHandle,
        runtime_setup: _RuntimeSetup,
        cancel_token: Callable[[], bool] | None,
    ) -> _RuntimeExecutionPhase:
        """Execute the main CLI loop and post-run review/lint phases."""
        redactor = SecretRedactor(list((request.secrets or {}).values()))
        execution = run_cli_runtime_loop(
            self.runtime_adapter,  # type: ignore[attr-defined]
            runtime_setup.session,
            system_prompt=runtime_setup.system_prompt,
            settings=runtime_setup.runtime_settings,
            tool_registry=self.tool_registry,  # type: ignore[attr-defined]
            granted_permission=runtime_setup.granted_permission,
            working_directory=workspace.repo_path,
            cancel_token=cancel_token,
            task_id=request.task_id,
            session_id=request.session_id,
            model_name=getattr(self.runtime_adapter, "model", None),  # type: ignore[attr-defined]
            redactor=redactor,
            response_format=request.response_format,
            response_schema=request.response_schema,
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

        review_result, files_changed, lint_format_result, lint_format_artifacts = (
            self._run_self_review_if_successful(
                request=request,
                workspace=workspace,
                runtime_setup=runtime_setup,
                execution=execution,
                files_changed=files_changed,
                lint_format_result=lint_format_result,
                lint_format_artifacts=lint_format_artifacts,
                cancel_token=cancel_token,
            )
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

        set_span_status_from_outcome(result.status, result.summary)
        return result

    def _resolve_runtime_mode(self, request: WorkerRequest) -> WorkerRuntimeMode:
        """Resolve effective runtime mode from request override or worker defaults."""
        if request.runtime_mode is not None:
            return request.runtime_mode
        return self.default_runtime_mode  # type: ignore[attr-defined]

    def _runtime_mode_not_supported_result(self, runtime_mode: WorkerRuntimeMode) -> WorkerResult:
        """Return a structured failure for unsupported execution runtime modes."""
        return WorkerResult(
            status="failure",
            summary=(
                "GeminiCliWorker does not support runtime mode "
                f"`{runtime_mode.value}`. Supported modes: native_agent, tool_loop."
            ),
            failure_kind="provider_error",
            next_action_hint="inspect_worker_configuration",
        )
