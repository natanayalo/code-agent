"""Durable decisions for the Temporal verification and review completion loop."""

from __future__ import annotations

from typing import Literal

from orchestrator.nodes.verification_result import VERIFIER_REPAIR_REQUEST_CONSTRAINT
from orchestrator.review import REPAIR_REQUEST_CONSTRAINT
from orchestrator.state import CompletionRepairSource, OrchestratorModel, OrchestratorState


class CompletionLoopDecision(OrchestratorModel):
    """Compact Activity result that deterministically advances the workflow."""

    continuation: Literal["complete", "repair", "manual_follow_up"]
    repair_source: CompletionRepairSource | None = None
    repair_pass: int = 0
    summary: str


def _repair_source(state: OrchestratorState) -> CompletionRepairSource | None:
    constraints = state.task.constraints if isinstance(state.task.constraints, dict) else {}
    if constraints.get(VERIFIER_REPAIR_REQUEST_CONSTRAINT):
        return "verifier"
    if constraints.get(REPAIR_REQUEST_CONSTRAINT):
        return "independent_review"
    return state.completion_loop.repair_source


def _decision_summary(
    state: OrchestratorState,
    continuation: Literal["complete", "repair", "manual_follow_up"],
) -> str:
    if state.completion_loop.summary:
        return state.completion_loop.summary
    if continuation == "manual_follow_up" and state.result and state.result.summary:
        return state.result.summary
    if continuation == "repair":
        source = _repair_source(state) or "verification"
        return f"{source.replace('_', ' ')} requested bounded repair"
    if state.review is not None:
        return state.review.summary
    if state.verification is not None:
        return state.verification.summary or "verification completed"
    return "verification and review completed"


def decision_from_state(state: OrchestratorState) -> CompletionLoopDecision:
    """Reconstruct the latest workflow decision from the durable snapshot."""
    phase = state.completion_loop.phase
    if phase == "repair_requested" or (phase == "initial" and state.repair_handoff_requested):
        continuation: Literal["complete", "repair", "manual_follow_up"] = "repair"
    elif phase == "manual_follow_up" or (
        state.result is not None and state.result.next_action_hint == "await_manual_follow_up"
    ):
        continuation = "manual_follow_up"
    else:
        continuation = "complete"
    return CompletionLoopDecision(
        continuation=continuation,
        repair_source=_repair_source(state),
        repair_pass=state.completion_loop.repair_pass,
        summary=_decision_summary(state, continuation),
    )


def apply_verification_decision(state: OrchestratorState) -> CompletionLoopDecision:
    """Persist the node outcome as the next completion-loop phase."""
    if state.repair_handoff_requested:
        source = _repair_source(state)
        repair_pass = state.completion_loop.repair_pass + 1
        repair_label = (source or "verification").replace("_", " ")
        summary = f"{repair_label} requested repair pass {repair_pass}"
        state.completion_loop = state.completion_loop.model_copy(
            update={
                "phase": "repair_requested",
                "repair_pass": repair_pass,
                "repair_source": source,
                "summary": summary,
            }
        )
    elif state.result is not None and state.result.next_action_hint == "await_manual_follow_up":
        state.repair_handoff_requested = False
        state.completion_loop = state.completion_loop.model_copy(
            update={
                "phase": "manual_follow_up",
                "repair_source": _repair_source(state),
                "summary": state.result.summary or "Manual follow-up is required.",
            }
        )
    else:
        state.repair_handoff_requested = False
        if state.review is not None:
            summary = state.review.summary
        elif state.verification is not None:
            summary = state.verification.summary or "verification completed"
        else:
            summary = "verification and review completed"
        state.completion_loop = state.completion_loop.model_copy(
            update={"phase": "complete", "summary": summary}
        )
    return decision_from_state(state)


def apply_repair_rejection(state: OrchestratorState) -> CompletionLoopDecision:
    """Convert a rejected repair permission request into a durable manual handoff."""
    constraints = dict(state.task.constraints)
    constraints.pop(VERIFIER_REPAIR_REQUEST_CONSTRAINT, None)
    constraints.pop(REPAIR_REQUEST_CONSTRAINT, None)
    state.task = state.task.model_copy(update={"constraints": constraints})
    summary = "Repair permission was rejected by the operator; manual follow-up is required."
    if state.result is not None:
        result_summary = state.result.summary
        state.result = state.result.model_copy(
            update={
                "status": "failure",
                "failure_kind": "incomplete_delivery",
                "summary": f"{result_summary}\n\n{summary}" if result_summary else summary,
                "next_action_hint": "await_manual_follow_up",
            }
        )
    state.repair_handoff_requested = False
    state.completion_loop = state.completion_loop.model_copy(
        update={"phase": "manual_follow_up", "summary": summary}
    )
    return decision_from_state(state)


def verification_is_pending(state: OrchestratorState, *, has_prior_event: bool) -> bool:
    """Return whether this Activity invocation owns a new logical verification pass."""
    if state.completion_loop.phase == "verification_pending":
        return True
    return state.completion_loop.phase == "initial" and not has_prior_event
