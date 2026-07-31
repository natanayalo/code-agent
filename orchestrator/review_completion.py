"""Terminal completion projection for unresolved independent-review findings."""

from __future__ import annotations

from typing import Any

from orchestrator.state import OrchestratorState
from workers.review import ReviewResult

REPAIR_REQUEST_CONSTRAINT = "independent_review_repair_request"
SKIP_INDEPENDENT_REVIEW_CONSTRAINT = "skip_independent_review"


def manual_review_handoff_update(
    state: OrchestratorState,
    parsed_review: ReviewResult,
) -> dict[str, Any]:
    """Project unresolved actionable findings as an explicit manual handoff."""
    constraints = dict(state.task.constraints)
    constraints.pop(REPAIR_REQUEST_CONSTRAINT, None)
    constraints.pop(SKIP_INDEPENDENT_REVIEW_CONSTRAINT, None)
    updated_task = state.task.model_copy(update={"constraints": constraints})
    finding_count = len(parsed_review.findings)
    finding_label = "finding" if finding_count == 1 else "findings"
    prior_summary = state.result.summary if state.result is not None else None
    summary_parts = [
        prior_summary,
        (
            f"Independent review still has {finding_count} actionable {finding_label}; "
            "bounded repair is unavailable or exhausted. Manual follow-up is required."
        ),
        parsed_review.summary,
    ]
    updated_result = (
        state.result.model_copy(
            update={
                "status": "failure",
                "failure_kind": "incomplete_delivery",
                "summary": "\n\n".join(part for part in summary_parts if part),
                "next_action_hint": "await_manual_follow_up",
            }
        )
        if state.result is not None
        else None
    )
    return {
        "current_step": "review_result",
        "task": updated_task.model_dump(),
        "result": updated_result.model_dump() if updated_result is not None else None,
        "review": parsed_review.model_dump(),
        "repair_handoff_requested": False,
        "progress_updates": [
            *state.progress_updates,
            "independent review requires manual follow-up",
        ],
    }
