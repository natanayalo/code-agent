"""Unified runtime executor coordinating the iterative tool loop, linting, and self-review."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.observability import set_span_status_from_outcome
from sandbox import DockerSandboxContainer, WorkspaceHandle
from sandbox.redact import SecretRedactor
from tools import (
    EXECUTE_BASH_TOOL_NAME,
    ToolExpectedArtifact,
    ToolPermissionLevel,
    ToolRegistry,
    granted_permission_from_constraints,
)
from workers.base import ArtifactReference, WorkerRequest, WorkerResult
from workers.cli_adapter_utils import build_worker_result
from workers.cli_runtime import (
    CliRuntimeAdapter,
    CliRuntimeExecutionResult,
    CliRuntimeSettings,
    ShellSessionProtocol,
    run_cli_runtime_loop,
    settings_from_budget,
)
from workers.post_run_lint import collect_changed_files_and_apply_post_run_lint_format
from workers.prompt import build_system_prompt
from workers.review import ReviewResult
from workers.sandbox_adapter import SandboxSessionAdapter
from workers.self_review import collect_diff_for_review, run_shared_self_review_fix_loop

logger = logging.getLogger(__name__)


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
    lint_format_result: dict[str, Any] | None
    lint_format_artifacts: list[ArtifactReference]
    review_result: ReviewResult | None


def _workspace_artifacts(workspace: WorkspaceHandle) -> list[ArtifactReference]:
    """Build the default artifact references for a retained workspace."""
    return [
        ArtifactReference(
            name="workspace",
            uri=workspace.workspace_path.as_uri(),
            artifact_type="workspace",
        )
    ]


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
    post_run_lint_format: dict[str, Any] | None = None,
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


class RuntimeExecutor:
    """Unified runtime executor driving the tool loop execution."""

    def __init__(
        self,
        *,
        runtime_adapter: CliRuntimeAdapter,
        tool_registry: ToolRegistry,
        sandbox_adapter: SandboxSessionAdapter,
        runtime_settings: CliRuntimeSettings,
    ) -> None:
        self.runtime_adapter = runtime_adapter
        self.tool_registry = tool_registry
        self.sandbox_adapter = sandbox_adapter
        self.runtime_settings = runtime_settings

    def _prepare_setup(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        container: DockerSandboxContainer,
        session: ShellSessionProtocol,
        system_prompt_override: str | None,
        read_only_workspace: bool,
    ) -> _RuntimeSetup:
        runtime_settings = settings_from_budget(
            request.budget,
            defaults=self.runtime_settings,
            task_id=request.task_id,
            session_id=request.session_id,
            read_only=read_only_workspace,
        )
        constraints = request.constraints if isinstance(request.constraints, dict) else {}
        granted_permission = granted_permission_from_constraints(constraints)
        bash_tool = self.tool_registry.require_tool(EXECUTE_BASH_TOOL_NAME)
        fallback_command_template = (
            constraints.get("post_run_lint_format_command")
            if isinstance(constraints.get("post_run_lint_format_command"), str)
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

    def _execute_loop(
        self,
        runtime_setup: _RuntimeSetup,
        workspace: WorkspaceHandle,
        request: WorkerRequest,
        cancel_token: Callable[[], bool] | None,
    ) -> CliRuntimeExecutionResult:
        redactor = SecretRedactor(list((request.secrets or {}).values()))
        return run_cli_runtime_loop(
            self.runtime_adapter,
            runtime_setup.session,
            system_prompt=runtime_setup.system_prompt,
            settings=runtime_setup.runtime_settings,
            tool_registry=self.tool_registry,
            granted_permission=runtime_setup.granted_permission,
            working_directory=workspace.repo_path,
            cancel_token=cancel_token,
            task_id=request.task_id,
            session_id=request.session_id,
            model_name=getattr(self.runtime_adapter, "model", None),
            redactor=redactor,
            response_format=request.response_format,
            response_schema=request.response_schema,
        )

    def _apply_lint_format(
        self,
        runtime_setup: _RuntimeSetup,
        workspace: WorkspaceHandle,
        execution: CliRuntimeExecutionResult,
    ) -> tuple[list[str], dict[str, Any] | None, list[ArtifactReference]]:
        return collect_changed_files_and_apply_post_run_lint_format(
            session=runtime_setup.session,
            execution=execution,
            expect_changed_files_artifact=runtime_setup.expects_changed_files,
            repo_path_for_detection=workspace.repo_path,
            repo_working_directory=Path(runtime_setup.container.working_dir),
            timeout_seconds=runtime_setup.runtime_settings.command_timeout_seconds,
            fallback_command_template=runtime_setup.fallback_command_template,
        )

    def _run_self_review(
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
        constraints = request.constraints if isinstance(request.constraints, dict) else {}
        return run_shared_self_review_fix_loop(
            execution=execution,
            task_text=request.task_text,
            constraints=constraints,
            runtime_adapter=self.runtime_adapter,
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
            tool_registry=self.tool_registry,
            granted_permission=runtime_setup.granted_permission,
            session=runtime_setup.session,
            cancel_token=cancel_token,
            task_id=request.task_id,
            session_id=request.session_id,
            model_name=getattr(self.runtime_adapter, "model", None),
            adapter_failure_log_message=(
                "CLI worker self-review adapter failed; recording explicit " "no-findings fallback."
            ),
            adapter_failure_logger=logger,
            check_cancel_before_review=True,
        )

    def _resolve_setup_parameters(self, request: WorkerRequest) -> tuple[dict[str, str], bool]:
        tool_names = request.tools
        if tool_names is None:
            tool_names = [tool.name for tool in self.tool_registry.list_tools()]

        scoped_secrets = self.tool_registry.get_scoped_secrets(
            tool_names=tool_names,
            available_secrets=request.secrets,
        )

        network_enabled = bool(request.network_enabled)
        if not network_enabled:
            constraints = request.constraints if isinstance(request.constraints, dict) else {}
            granted_permission = granted_permission_from_constraints(constraints)
            if granted_permission == ToolPermissionLevel.NETWORKED_WRITE:
                for name in tool_names:
                    if (tool := self.tool_registry.get_tool(name)) and tool.network_required:
                        network_enabled = True
                        break

        return scoped_secrets, network_enabled

    def execute(
        self,
        request: WorkerRequest,
        *,
        workspace: WorkspaceHandle,
        system_prompt_override: str | None = None,
        cancel_token: Callable[[], bool] | None = None,
    ) -> WorkerResult:
        """Set up, execute, review, and finalize a CLI tool loop run."""
        scoped_secrets, network_enabled = self._resolve_setup_parameters(request)
        constraints = request.constraints if isinstance(request.constraints, dict) else {}
        read_only_workspace = bool(request.read_only) or bool(constraints.get("read_only"))
        with self.sandbox_adapter.session_context(
            workspace=workspace,
            environment=scoped_secrets,
            network_enabled=network_enabled,
            read_only_workspace=read_only_workspace,
            secrets=request.secrets,
            scratch_namespace=request.scratch_namespace,
        ) as (container, session):
            runtime_setup = self._prepare_setup(
                request, workspace, container, session, system_prompt_override, read_only_workspace
            )
            execution = self._execute_loop(runtime_setup, workspace, request, cancel_token)
            files_changed: list[str]
            lint_format_result: dict[str, Any] | None
            lint_format_artifacts: list[ArtifactReference]

            if cancel_token and cancel_token():
                files_changed, lint_format_result, lint_format_artifacts = [], None, []
            else:
                files_changed, lint_format_result, lint_format_artifacts = self._apply_lint_format(
                    runtime_setup, workspace, execution
                )

            review_result = None
            if execution.status == "success" and not (cancel_token and cancel_token()):
                review_result, files_changed, lint_format_result, lint_format_artifacts = (
                    self._run_self_review(
                        request,
                        workspace,
                        runtime_setup,
                        execution,
                        files_changed,
                        lint_format_result,
                        lint_format_artifacts,
                        cancel_token,
                    )
                )

            runtime_phase = _RuntimeExecutionPhase(
                execution=execution,
                files_changed=files_changed,
                lint_format_result=lint_format_result,
                lint_format_artifacts=lint_format_artifacts,
                review_result=review_result,
            )

            return self._finalize_result(workspace, runtime_setup, runtime_phase, cancel_token)

    def _finalize_result(
        self,
        workspace: WorkspaceHandle,
        runtime_setup: _RuntimeSetup,
        runtime_phase: _RuntimeExecutionPhase,
        cancel_token: Callable[[], bool] | None,
    ) -> WorkerResult:
        result = _worker_result_from_execution(
            workspace,
            runtime_phase.execution,
            files_changed=runtime_phase.files_changed,
            post_run_lint_format=runtime_phase.lint_format_result,
            review_result=runtime_phase.review_result,
            diff_text=collect_diff_for_review(
                workspace.repo_path,
                timeout_seconds=runtime_setup.runtime_settings.command_timeout_seconds,
            )
            if runtime_phase.execution.status == "success" and not (cancel_token and cancel_token())
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
