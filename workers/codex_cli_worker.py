"""Codex CLI worker backed by the shared CLI runtime."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from functools import partial
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
from sandbox.workspace import _mask_url_credentials, default_workspace_root
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
    run_cli_runtime_loop,
    settings_from_budget,
)
from workers.failure_taxonomy import classify_failure_kind
from workers.post_run_lint import (
    collect_changed_files_and_apply_post_run_lint_format,
    merge_post_run_lint_results,
)
from workers.prompt import build_system_prompt
from workers.review import ReviewResult
from workers.self_review import (
    build_fix_loop_prompt,
    build_self_review_prompt,
    collect_diff_for_review,
    fallback_no_findings_review,
    merge_budget_ledgers,
    parse_review_result,
    remaining_runtime_settings,
    resolve_self_review_max_fix_iterations,
    should_skip_self_review,
)

logger = logging.getLogger(__name__)


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
    if execution.stop_reason in {"max_iterations", "worker_timeout", "budget_exceeded"}:
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

    async def run(
        self, request: WorkerRequest, *, system_prompt: str | None = None
    ) -> WorkerResult:
        """Provision a workspace, run the CLI loop, and return a typed result."""
        cancel_event = threading.Event()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            None,
            partial(
                self._run_sync,
                request,
                cancel_token=cancel_event.is_set,
                system_prompt_override=system_prompt,
            ),
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=10.0)
            except TimeoutError as exc:
                raise asyncio.CancelledError("Graceful shutdown of sync worker timed out.") from exc

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

    def _run_sync(
        self,
        request: WorkerRequest,
        cancel_token: Callable[[], bool] | None = None,
        system_prompt_override: str | None = None,
    ) -> WorkerResult:
        """Provision a workspace, run the CLI runtime, and return a typed result."""
        if request.repo_url is None or not request.repo_url.strip():
            return WorkerResult(
                status="error",
                summary=(
                    "CodexCliWorker requires a non-empty repo_url to provision a sandbox workspace."
                ),
                failure_kind="unknown",
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
                failure_kind="sandbox_infra",
                next_action_hint="inspect_worker_configuration",
            )

        result: WorkerResult | None = None
        run_succeeded = False
        container: DockerSandboxContainer | None = None
        session: ShellSessionProtocol | None = None

        try:
            # Scope secrets: only inject those required by the tools available for this run.
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
            # Redact ALL secrets: the session redactor should know about every secret
            # provided by the user, even if they weren't injected into the environment.
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
            execution = run_cli_runtime_loop(
                self.runtime_adapter,
                session,
                system_prompt=system_prompt,
                settings=runtime_settings,
                tool_registry=self.tool_registry,
                granted_permission=granted_permission,
                working_directory=workspace.repo_path,
                cancel_token=cancel_token,
                model_name=getattr(self.runtime_adapter, "model", None),
            )

            expects_changed_files = (
                ToolExpectedArtifact.CHANGED_FILES in bash_tool.expected_artifacts
            )
            files_changed, lint_format_result, lint_format_artifacts = (
                collect_changed_files_and_apply_post_run_lint_format(
                    session=session,
                    execution=execution,
                    expect_changed_files_artifact=expects_changed_files,
                    repo_path_for_detection=workspace.repo_path,
                    repo_working_directory=Path(container.working_dir),
                    timeout_seconds=runtime_settings.command_timeout_seconds,
                    fallback_command_template=fallback_command_template,
                )
            )
            review_result: ReviewResult | None = None
            if execution.status == "success" and not should_skip_self_review(request.constraints):
                max_fix_iterations = resolve_self_review_max_fix_iterations(request.constraints)
                for review_attempt in range(max_fix_iterations + 1):
                    diff_text = collect_diff_for_review(
                        workspace.repo_path,
                        timeout_seconds=runtime_settings.command_timeout_seconds,
                    )
                    review_prompt = build_self_review_prompt(
                        task_text=request.task_text,
                        worker_summary=execution.summary,
                        files_changed=files_changed,
                        diff_text=diff_text,
                        repo_path=workspace.repo_path,
                        commands_run=execution.commands_run,
                    )

                    try:
                        review_step = self.runtime_adapter.next_step(
                            (),
                            prompt_override=review_prompt,
                            working_directory=workspace.repo_path,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Codex CLI worker self-review adapter failed; recording explicit "
                            "no-findings fallback.",
                            exc_info=exc,
                        )
                        review_result = fallback_no_findings_review(
                            "Worker self-review failed to return a structured payload."
                        )
                        break

                    if review_step.kind != "final" or review_step.final_output is None:
                        review_result = fallback_no_findings_review(
                            "Worker self-review returned a non-final response."
                        )
                        break

                    parsed_review_result = parse_review_result(review_step.final_output)
                    if parsed_review_result is None:
                        review_result = fallback_no_findings_review(
                            "Worker self-review returned an invalid structured payload."
                        )
                        break

                    review_result = parsed_review_result
                    if review_result.outcome == "no_findings":
                        break
                    if review_attempt >= max_fix_iterations:
                        break

                    follow_up_settings = remaining_runtime_settings(
                        runtime_settings,
                        budget_ledger=execution.budget_ledger,
                    )
                    if follow_up_settings is None:
                        execution.status = "failure"
                        execution.summary = (
                            "CLI runtime exhausted its remaining budget before applying "
                            "self-review fixes."
                        )
                        execution.stop_reason = "budget_exceeded"
                        break

                    follow_up_execution = run_cli_runtime_loop(
                        self.runtime_adapter,
                        session,
                        system_prompt=build_fix_loop_prompt(
                            base_system_prompt=system_prompt,
                            review_result=review_result,
                        ),
                        settings=follow_up_settings,
                        tool_registry=self.tool_registry,
                        granted_permission=granted_permission,
                        working_directory=workspace.repo_path,
                        cancel_token=cancel_token,
                        model_name=getattr(self.runtime_adapter, "model", None),
                    )
                    merge_budget_ledgers(execution.budget_ledger, follow_up_execution.budget_ledger)
                    execution.commands_run.extend(follow_up_execution.commands_run)
                    execution.messages.extend(follow_up_execution.messages)
                    execution.status = follow_up_execution.status
                    execution.summary = follow_up_execution.summary
                    execution.stop_reason = follow_up_execution.stop_reason
                    execution.permission_decision = follow_up_execution.permission_decision
                    if execution.status != "success":
                        break

                    files_changed, new_lint_format_result, new_lint_format_artifacts = (
                        collect_changed_files_and_apply_post_run_lint_format(
                            session=session,
                            execution=execution,
                            expect_changed_files_artifact=expects_changed_files,
                            repo_path_for_detection=workspace.repo_path,
                            repo_working_directory=Path(container.working_dir),
                            existing_files_changed=files_changed,
                            timeout_seconds=runtime_settings.command_timeout_seconds,
                            fallback_command_template=fallback_command_template,
                        )
                    )
                    lint_format_result = merge_post_run_lint_results(
                        lint_format_result,
                        new_lint_format_result,
                    )
                    lint_format_artifacts.extend(new_lint_format_artifacts)

            result = _worker_result_from_execution(
                workspace,
                execution,
                files_changed=files_changed,
                post_run_lint_format=lint_format_result,
                review_result=review_result,
                diff_text=collect_diff_for_review(
                    workspace.repo_path,
                    timeout_seconds=runtime_settings.command_timeout_seconds,
                )
                if execution.status == "success"
                else None,
                artifacts=lint_format_artifacts,
            )
            if cancel_token and cancel_token():
                result.status = "error"
                result.summary = "CLI runtime loop was cancelled by the orchestrator timeout."
                result.failure_kind = "timeout"
                result.next_action_hint = "inspect_workspace_artifacts"
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
