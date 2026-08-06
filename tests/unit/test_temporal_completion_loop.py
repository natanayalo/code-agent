"""Unit tests for orchestrator/temporal completion_loop, node_wave, policy, and queues."""

from __future__ import annotations

from datetime import timedelta

import pytest

from orchestrator.node_execution import NodeActivityRequest, logical_activity_key
from orchestrator.nodes.verification_result import VERIFIER_REPAIR_REQUEST_CONSTRAINT
from orchestrator.review import REPAIR_REQUEST_CONSTRAINT
from orchestrator.state import (
    CompletionLoopState,
    OrchestratorState,
    WorkerResult,
)
from orchestrator.temporal.completion_loop import (
    _decision_summary,
    _repair_source,
    apply_repair_rejection,
    apply_verification_decision,
    decision_from_state,
    verification_is_pending,
)
from orchestrator.temporal.node_wave import (
    NodeWaveItem,
    deterministic_wave_id,
)
from orchestrator.temporal.policy import (
    activity_options,
)
from orchestrator.temporal.queues import (
    DEFAULT_TEMPORAL_TASK_QUEUE,
    execution_task_queue_for_profile,
)

# ---------------------------------------------------------------------------
# completion_loop tests
# ---------------------------------------------------------------------------


def test_repair_source_verifier_constraint():
    state = OrchestratorState(
        task={
            "task_text": "t",
            "repo_url": "url",
            "constraints": {VERIFIER_REPAIR_REQUEST_CONSTRAINT: True},
        }
    )
    assert _repair_source(state) == "verifier"


def test_repair_source_review_constraint():
    state = OrchestratorState(
        task={"task_text": "t", "repo_url": "url", "constraints": {REPAIR_REQUEST_CONSTRAINT: True}}
    )
    assert _repair_source(state) == "independent_review"


def test_repair_source_fallback_to_completion_loop_state():
    state = OrchestratorState(
        task={"task_text": "t", "repo_url": "url"},
        completion_loop=CompletionLoopState(repair_source="verifier"),
    )
    assert _repair_source(state) == "verifier"


def test_decision_summary_existing_loop_summary():
    state = OrchestratorState(
        task={"task_text": "t", "repo_url": "url"},
        completion_loop=CompletionLoopState(summary="explicit summary"),
    )
    assert _decision_summary(state, "complete") == "explicit summary"


def test_decision_summary_manual_follow_up_result_summary():
    state = OrchestratorState(
        task={"task_text": "t", "repo_url": "url"},
        result=WorkerResult(status="failure", summary="failure reason"),
    )
    assert _decision_summary(state, "manual_follow_up") == "failure reason"


def test_decision_summary_repair():
    state = OrchestratorState(
        task={
            "task_text": "t",
            "repo_url": "url",
            "constraints": {VERIFIER_REPAIR_REQUEST_CONSTRAINT: True},
        }
    )
    assert "verifier requested bounded repair" in _decision_summary(state, "repair")


def test_decision_from_state_repair_requested():
    state = OrchestratorState(
        task={"task_text": "t", "repo_url": "url"},
        completion_loop=CompletionLoopState(phase="repair_requested", repair_pass=1),
    )
    decision = decision_from_state(state)
    assert decision.continuation == "repair"
    assert decision.repair_pass == 1


def test_decision_from_state_manual_follow_up():
    state = OrchestratorState(
        task={"task_text": "t", "repo_url": "url"},
        completion_loop=CompletionLoopState(phase="manual_follow_up"),
    )
    decision = decision_from_state(state)
    assert decision.continuation == "manual_follow_up"


def test_apply_verification_decision_repair_handoff():
    state = OrchestratorState(
        task={
            "task_text": "t",
            "repo_url": "url",
            "constraints": {VERIFIER_REPAIR_REQUEST_CONSTRAINT: True},
        },
        repair_handoff_requested=True,
    )
    decision = apply_verification_decision(state)
    assert decision.continuation == "repair"
    assert state.completion_loop.phase == "repair_requested"
    assert state.completion_loop.repair_pass == 1


def test_apply_verification_decision_manual_follow_up():
    state = OrchestratorState(
        task={"task_text": "t", "repo_url": "url"},
        result=WorkerResult(
            status="failure",
            next_action_hint="await_manual_follow_up",
            summary="manual help needed",
        ),
    )
    decision = apply_verification_decision(state)
    assert decision.continuation == "manual_follow_up"
    assert state.completion_loop.phase == "manual_follow_up"


def test_apply_verification_decision_complete():
    state = OrchestratorState(task={"task_text": "t", "repo_url": "url"})
    decision = apply_verification_decision(state)
    assert decision.continuation == "complete"
    assert state.completion_loop.phase == "complete"


def test_apply_repair_rejection():
    state = OrchestratorState(
        task={
            "task_text": "t",
            "repo_url": "url",
            "constraints": {VERIFIER_REPAIR_REQUEST_CONSTRAINT: True},
        },
        result=WorkerResult(status="failure", summary="test failed"),
    )
    decision = apply_repair_rejection(state)
    assert decision.continuation == "manual_follow_up"
    assert state.result.next_action_hint == "await_manual_follow_up"
    assert "Repair permission was rejected" in state.result.summary


def test_verification_is_pending():
    state = OrchestratorState(
        task={"task_text": "t", "repo_url": "url"},
        completion_loop=CompletionLoopState(phase="verification_pending"),
    )
    assert verification_is_pending(state, has_prior_event=True) is True

    state_initial = OrchestratorState(
        task={"task_text": "t", "repo_url": "url"},
        completion_loop=CompletionLoopState(phase="initial"),
    )
    assert verification_is_pending(state_initial, has_prior_event=False) is True
    assert verification_is_pending(state_initial, has_prior_event=True) is False


# ---------------------------------------------------------------------------
# node_wave & policy & queues tests
# ---------------------------------------------------------------------------


def test_deterministic_wave_id():
    digest1 = "a" * 64
    digest2 = "b" * 64
    k1 = logical_activity_key("p1", "n1", 1)
    k2 = logical_activity_key("p1", "n2", 1)
    req1 = NodeActivityRequest(
        task_id="t1",
        plan_id="p1",
        node_id="n1",
        logical_attempt=1,
        logical_activity_key=k1,
        effective_input_digest=digest1,
    )
    req2 = NodeActivityRequest(
        task_id="t1",
        plan_id="p1",
        node_id="n2",
        logical_attempt=1,
        logical_activity_key=k2,
        effective_input_digest=digest2,
    )

    item1 = NodeWaveItem(node_id="n1", activity_request=req1, execution_task_queue="q1")
    item2 = NodeWaveItem(node_id="n2", activity_request=req2, execution_task_queue="q1")

    wave_id1 = deterministic_wave_id("p1", [item1, item2])
    wave_id2 = deterministic_wave_id("p1", [item1, item2])
    assert wave_id1 == wave_id2
    assert wave_id1.startswith("node-wave:v2:p1:")


def test_activity_options():
    opts = activity_options("classify_and_plan")
    assert "start_to_close_timeout" in opts
    assert opts["start_to_close_timeout"] == timedelta(minutes=5)

    opts_worker = activity_options("run_worker", task_queue="custom-queue")
    assert opts_worker["task_queue"] == "custom-queue"

    with pytest.raises(ValueError, match="Unknown Temporal activity policy"):
        activity_options("unknown_activity")

    with pytest.raises(
        ValueError, match="Only execution activities may select a Temporal task queue"
    ):
        activity_options("classify_and_plan", task_queue="custom-queue")


def test_queues():
    assert DEFAULT_TEMPORAL_TASK_QUEUE == "task-execution-queue"
    assert execution_task_queue_for_profile("codex-fast") == "code-agent-codex"
    assert execution_task_queue_for_profile("other") == "task-execution-queue"
