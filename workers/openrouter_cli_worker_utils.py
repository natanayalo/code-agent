from __future__ import annotations

import logging
import re

from sandbox.workspace import WorkspaceHandle
from workers.base import ArtifactReference, WorkerRequest, WorkerResult
from workers.cli_adapter_utils import build_worker_result
from workers.cli_runtime import CliRuntimeExecutionResult
from workers.review import ReviewResult

logger = logging.getLogger(__name__)


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for sandbox bookkeeping."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _workspace_task_id(request: WorkerRequest) -> str:
    """Build a readable workspace task identifier from the worker request."""
    if request.task_id:
        return request.task_id
    source = request.session_id or request.task_text
    return f"openrouter-cli-{_slugify(source)}"


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
        else "OpenRouterCliWorker cleaned up the workspace per policy."
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
        "OpenRouter CLI worker failed inside a provisioned workspace",
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
