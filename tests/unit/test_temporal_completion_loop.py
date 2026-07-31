"""Unit coverage for durable Temporal completion-loop decisions."""

from orchestrator.state import OrchestratorState
from orchestrator.temporal.completion_loop import (
    apply_repair_rejection,
    apply_verification_decision,
    decision_from_state,
    verification_is_pending,
)


def test_verifier_repair_decision_advances_durable_phase() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "repair verifier failure",
                "constraints": {"independent_verifier_repair_request": "fix tests"},
            },
            "repair_handoff_requested": True,
        }
    )

    decision = apply_verification_decision(state)

    assert decision.continuation == "repair"
    assert decision.repair_source == "verifier"
    assert decision.repair_pass == 1
    assert state.completion_loop.phase == "repair_requested"


def test_manual_follow_up_decision_preserves_actionable_summary() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "review changes"},
            "result": {
                "status": "failure",
                "failure_kind": "incomplete_delivery",
                "summary": "Review findings remain; manual follow-up is required.",
                "next_action_hint": "await_manual_follow_up",
            },
            "completion_loop": {
                "repair_pass": 1,
                "repair_source": "independent_review",
            },
        }
    )

    decision = apply_verification_decision(state)

    assert decision.continuation == "manual_follow_up"
    assert decision.repair_source == "independent_review"
    assert decision.summary == "Review findings remain; manual follow-up is required."
    assert state.completion_loop.phase == "manual_follow_up"


def test_persisted_repair_decision_is_reconstructed_without_new_pass() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "repair verifier failure"},
            "completion_loop": {
                "phase": "repair_requested",
                "repair_pass": 2,
                "repair_source": "verifier",
                "summary": "verifier requested repair pass 2",
            },
        }
    )

    decision = decision_from_state(state)

    assert decision.continuation == "repair"
    assert decision.repair_pass == 2
    assert decision.summary == "verifier requested repair pass 2"


def test_rejected_repair_permission_becomes_manual_follow_up() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "repair verifier failure",
                "constraints": {"independent_verifier_repair_request": "fix tests"},
            },
            "result": {
                "status": "failure",
                "summary": "workspace write permission required",
                "requested_permission": "workspace_write",
                "next_action_hint": "request_higher_permission",
            },
            "completion_loop": {
                "phase": "repair_requested",
                "repair_pass": 1,
                "repair_source": "verifier",
            },
        }
    )

    decision = apply_repair_rejection(state)

    assert decision.continuation == "manual_follow_up"
    assert state.result is not None
    assert state.result.failure_kind == "incomplete_delivery"
    assert state.result.next_action_hint == "await_manual_follow_up"
    assert "independent_verifier_repair_request" not in state.task.constraints


def test_only_initial_or_explicit_pending_phase_runs_verification() -> None:
    initial = OrchestratorState(task={"task_text": "initial"})
    pending = OrchestratorState.model_validate(
        {
            "task": {"task_text": "pending"},
            "completion_loop": {"phase": "verification_pending"},
        }
    )
    complete = OrchestratorState.model_validate(
        {
            "task": {"task_text": "complete"},
            "completion_loop": {"phase": "complete"},
        }
    )

    assert verification_is_pending(initial, has_prior_event=False) is True
    assert verification_is_pending(initial, has_prior_event=True) is False
    assert verification_is_pending(pending, has_prior_event=True) is True
    assert verification_is_pending(complete, has_prior_event=False) is False


def test_decision_summary_falls_back_to_manual_result() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "manual"},
            "result": {
                "status": "failure",
                "summary": "operator action required",
                "next_action_hint": "await_manual_follow_up",
            },
            "completion_loop": {"phase": "manual_follow_up"},
        }
    )

    assert decision_from_state(state).summary == "operator action required"


def test_decision_summary_describes_unpersisted_repair_request() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "repair",
                "constraints": {"independent_review_repair_request": "fix finding"},
            },
            "repair_handoff_requested": True,
        }
    )

    assert decision_from_state(state).summary == "independent review requested bounded repair"


def test_decision_summary_uses_review_or_verification_fallbacks() -> None:
    reviewed = OrchestratorState.model_validate(
        {
            "task": {"task_text": "reviewed"},
            "review": {
                "reviewer_kind": "independent_reviewer",
                "summary": "review accepted",
                "confidence": 0.9,
                "outcome": "no_findings",
            },
        }
    )
    verified = OrchestratorState.model_validate(
        {
            "task": {"task_text": "verified"},
            "verification": {"status": "passed", "summary": None},
        }
    )
    empty = OrchestratorState(task={"task_text": "empty"})

    assert decision_from_state(reviewed).summary == "review accepted"
    assert decision_from_state(verified).summary == "verification completed"
    assert decision_from_state(empty).summary == "verification and review completed"
