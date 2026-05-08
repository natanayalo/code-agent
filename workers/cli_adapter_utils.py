"""Shared high-level utilities for CLI runtime worker adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from workers.base import WorkerResult
from workers.failure_taxonomy import build_failure_summary, classify_failure_kind

if TYPE_CHECKING:
    from workers.base import ArtifactReference
    from workers.cli_runtime import CliRuntimeExecutionResult
    from workers.review import ReviewResult


def build_worker_result(
    *,
    execution: CliRuntimeExecutionResult,
    files_changed: list[str],
    requested_permission: str | None = None,
    post_run_lint_format: dict[str, object] | None = None,
    review_result: ReviewResult | None = None,
    diff_text: str | None = None,
    artifacts: list[ArtifactReference] | None = None,
    next_action_hint: str | None = None,
) -> WorkerResult:
    """Construct a standardized WorkerResult from CLI runtime execution outputs."""
    final_message = (
        execution.messages[-1].content
        if execution.messages and execution.messages[-1].role == "assistant"
        else None
    )

    summary = build_failure_summary(
        summary=execution.summary,
        final_message=final_message,
    )

    failure_kind = classify_failure_kind(
        status=execution.status,
        stop_reason=execution.stop_reason,
        summary=execution.summary,
        final_message=final_message,
        commands_run=execution.commands_run,
    )

    budget_usage = execution.budget_ledger.model_dump(mode="json")
    if post_run_lint_format is not None:
        budget_usage["post_run_lint_format"] = post_run_lint_format

    return WorkerResult(
        status=execution.status,
        summary=summary,
        failure_kind=failure_kind,
        requested_permission=requested_permission,
        budget_usage=budget_usage,
        commands_run=execution.commands_run,
        files_changed=files_changed,
        artifacts=artifacts or [],
        review_result=review_result,
        diff_text=diff_text,
        next_action_hint=next_action_hint,
    )
