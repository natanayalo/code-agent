"""Unit tests for hierarchical budget contracts, enforcement, and compliance evaluation."""

from __future__ import annotations

from datetime import UTC, datetime

from orchestrator.decomposition import is_read_only_fanout_eligible
from orchestrator.hierarchical_budget import (
    attach_hierarchical_budget_usage,
    check_pre_dispatch_budget_gate,
    compute_hierarchical_budget_ledger,
    effective_slice_budget,
    evaluate_hierarchical_budget_compliance,
    resolve_budget_spec,
    resolve_effective_review_repair_budget,
    resolve_effective_verifier_repair_budget,
)
from orchestrator.state import (
    CompletionLoopState,
    DecomposedTaskNode,
    NodeOutcome,
    OrchestratorState,
    TaskRequest,
    TaskSpec,
    TaskTimelineEventState,
    WorkerResult,
)


def _make_state(
    *,
    budget: dict | None = None,
    constraints: dict | None = None,
    attempt_count: int = 1,
    repair_pass: int = 0,
    node_outcomes: list[NodeOutcome] | None = None,
    timeline_events: list[TaskTimelineEventState] | None = None,
    result: WorkerResult | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        task=TaskRequest(
            task_id="task-123",
            task_text="Run test task",
            budget=budget or {},
            constraints=constraints or {},
        ),
        attempt_count=attempt_count,
        completion_loop=CompletionLoopState(repair_pass=repair_pass),
        node_outcomes=node_outcomes or [],
        timeline_events=timeline_events or [],
        result=result,
    )


def test_resolve_budget_spec_interactive_and_unattended():
    interactive_spec = resolve_budget_spec({}, channel="telegram")
    assert interactive_spec.execution_mode == "interactive"
    assert interactive_spec.max_task_wall_time_seconds == 1200
    assert interactive_spec.max_worker_calls == 6
    assert interactive_spec.max_concurrent_nodes == 2
    assert interactive_spec.primary.timeout_seconds == 600
    assert interactive_spec.primary.max_iterations == 8
    assert interactive_spec.node.timeout_seconds == 600
    assert interactive_spec.node.max_node_attempts == 2
    assert interactive_spec.repair.max_repair_passes == 2

    unattended_spec = resolve_budget_spec({}, channel="http")
    assert unattended_spec.execution_mode == "unattended"
    assert unattended_spec.max_task_wall_time_seconds == 600
    assert unattended_spec.max_worker_calls == 4
    assert unattended_spec.primary.timeout_seconds == 300
    assert unattended_spec.primary.max_iterations == 5
    assert unattended_spec.node.timeout_seconds == 300
    assert unattended_spec.node.max_node_attempts == 2


def test_resolve_budget_spec_scout_modes():
    scout_spec = resolve_budget_spec({}, constraints={"task_type": "scout"})
    assert scout_spec.execution_mode == "unattended"
    assert scout_spec.max_task_wall_time_seconds == 180
    assert scout_spec.max_worker_calls == 1
    assert scout_spec.max_concurrent_nodes == 1
    assert scout_spec.node.max_node_attempts == 1
    assert scout_spec.repair.max_repair_passes == 0

    deep_spec = resolve_budget_spec({}, constraints={"task_type": "scout", "scout_mode": "deep"})
    assert deep_spec.max_task_wall_time_seconds == 720
    assert deep_spec.max_worker_calls == 2
    assert deep_spec.max_concurrent_nodes == 1
    assert deep_spec.node.max_node_attempts == 1
    assert deep_spec.repair.max_repair_passes == 0


def test_resolve_budget_spec_clamping_and_global_caps():
    over_budget = {
        "max_task_wall_time_seconds": 9999,
        "max_worker_calls": 99,
        "max_concurrent_nodes": 10,
        "worker_timeout_seconds": 1500,
        "max_iterations": 100,
        "max_node_attempts": 10,
        "max_repair_passes": 10,
    }
    spec = resolve_budget_spec(over_budget, channel="http")
    assert spec.max_task_wall_time_seconds == 1800
    assert spec.max_worker_calls == 16
    assert spec.max_concurrent_nodes == 2
    assert spec.primary.timeout_seconds == 900
    assert spec.primary.max_iterations == 20
    assert spec.node.max_node_attempts == 3
    assert spec.repair.max_repair_passes == 4


def test_compute_hierarchical_budget_ledger_headroom_and_exhaustion():
    now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 20, 12, 10, 0, tzinfo=UTC)
    events = [
        TaskTimelineEventState(event_type="worker_dispatched", created_at=now),
        TaskTimelineEventState(event_type="worker_completed", created_at=t1),
    ]
    state = _make_state(
        budget={"max_task_wall_time_seconds": 600, "max_worker_calls": 2},
        attempt_count=1,
        timeline_events=events,
        result=WorkerResult(status="success", budget_usage={"wall_clock_seconds": 600.0}),
    )

    ledger = compute_hierarchical_budget_ledger(state, active_slice="primary")
    assert ledger.worker_calls_used == 1
    assert ledger.remaining_worker_calls == 1
    assert ledger.wall_clock_seconds_used == 600.0
    assert ledger.remaining_wall_time_seconds == 0.0
    assert ledger.exhausted_dimension == "max_task_wall_time_seconds"

    # Pre-dispatch gate should fail closed on wall time
    eligible, reason, gate_ledger = check_pre_dispatch_budget_gate(state, active_slice="primary")
    assert not eligible
    assert reason == "max_task_wall_time_seconds"


def test_pre_dispatch_budget_gate_worker_calls_exhausted():
    state = _make_state(
        budget={"max_worker_calls": 2},
        attempt_count=2,
        result=WorkerResult(status="failure"),
    )
    eligible, reason, ledger = check_pre_dispatch_budget_gate(state, active_slice="primary")
    assert not eligible
    assert reason == "max_worker_calls"
    assert ledger.remaining_worker_calls == 0


def test_effective_slice_budget_clamping():
    state = _make_state(
        budget={
            "worker_timeout_seconds": 300,
            "max_task_wall_time_seconds": 500,
            "repair_timeout_seconds": 200,
        },
        timeline_events=[
            TaskTimelineEventState(
                event_type="worker_completed",
                created_at=datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC),
            ),
            TaskTimelineEventState(
                event_type="worker_completed",
                created_at=datetime(2026, 10, 20, 12, 7, 0, tzinfo=UTC),  # 420s
            ),
        ],
    )
    eff = effective_slice_budget(state, active_slice="repair")
    # Remaining wall time is 500 - 420 = 80s
    assert eff["worker_timeout_seconds"] == 80
    assert "budget_spec" in eff
    assert "hierarchical_budget" in eff


def test_resolve_effective_repair_budgets_combined_passes_and_exhaustion():
    state = _make_state(
        budget={"max_repair_passes": 2, "max_worker_calls": 4},
        constraints={
            "independent_verifier_max_repair_passes": 2,
            "independent_verifier_repair_passes_used": 1,
            "independent_review_max_repair_passes": 2,
            "independent_review_repair_passes_used": 1,
        },
    )
    # Total repair passes used = 1 + 1 = 2, max is 2
    v_max, v_used = resolve_effective_verifier_repair_budget(state)
    assert v_used == 1
    assert v_max == 1  # Clamped to used (no more passes available)

    r_max, r_used = resolve_effective_review_repair_budget(state.task.constraints, state=state)
    assert r_used == 1
    assert r_max == 1  # Clamped to used (no more passes available)


def test_resolve_effective_repair_budgets_zero_worker_calls_left():
    state = _make_state(
        budget={"max_repair_passes": 3, "max_worker_calls": 2},
        attempt_count=2,
        result=WorkerResult(status="success"),
        constraints={
            "independent_verifier_max_repair_passes": 2,
            "independent_verifier_repair_passes_used": 0,
        },
    )
    # 2 calls already used out of max 2 -> remaining is 0
    v_max, v_used = resolve_effective_verifier_repair_budget(state)
    assert v_used == 0
    assert v_max == 0  # Cannot queue repair when worker calls exhausted


def test_concurrency_gating_in_read_only_fanout():
    dummy_spec = TaskSpec(
        task_type="scout",
        goal="g",
        acceptance_criteria=["a"],
    )
    node = DecomposedTaskNode(
        node_id="n1",
        title="t",
        task_spec=dummy_spec,
        node_kind="inspect",
        execution_mode="read_only",
        parallel_safe=True,
        aggregation_role="context",
        max_attempts=2,
    )
    # Default allowed (concurrency=2, remaining_calls=2)
    assert is_read_only_fanout_eligible(
        parent_read_only=True,
        selected_profile_mutation_policy="read_only",
        node=node,
        completed_node_ids=set(),
        has_unresolved_blocker=False,
        fanout_disabled=False,
        max_concurrent_nodes=2,
        remaining_worker_calls=2,
    )
    # Gated by concurrency limit
    assert not is_read_only_fanout_eligible(
        parent_read_only=True,
        selected_profile_mutation_policy="read_only",
        node=node,
        completed_node_ids=set(),
        has_unresolved_blocker=False,
        fanout_disabled=False,
        max_concurrent_nodes=1,
        remaining_worker_calls=2,
    )
    # Gated by remaining calls
    assert not is_read_only_fanout_eligible(
        parent_read_only=True,
        selected_profile_mutation_policy="read_only",
        node=node,
        completed_node_ids=set(),
        has_unresolved_blocker=False,
        fanout_disabled=False,
        max_concurrent_nodes=2,
        remaining_worker_calls=1,
    )


def test_attach_hierarchical_budget_usage_preserves_native_agent_and_nodes():
    state = _make_state(budget={"max_worker_calls": 4})
    initial_res = WorkerResult(
        status="success",
        budget_usage={
            "native_agent": {"model_execution": {"provider": "gemini", "model": "gemini-2.5-pro"}},
            "nodes": {"node_1": {"wall_clock_seconds": 12.5}},
            "node_count": 1,
            "wall_clock_seconds": 12.5,
        },
    )
    enriched = attach_hierarchical_budget_usage(initial_res, state, active_slice="primary")
    assert enriched.budget_usage["native_agent"]["model_execution"]["provider"] == "gemini"
    assert enriched.budget_usage["nodes"]["node_1"]["wall_clock_seconds"] == 12.5
    assert "hierarchical_budget" in enriched.budget_usage
    assert enriched.budget_usage["hierarchical_budget"]["max_worker_calls"] == 4
    # Current call's wall clock seconds should be forwarded into hierarchical ledger
    assert enriched.budget_usage["hierarchical_budget"]["wall_clock_seconds_used"] == 12.5


def test_initial_dispatch_budget_gate_allows_single_call_scout():
    """Initial dispatch with attempt_count=1 and max_worker_calls=1 must not fail-closed."""
    state = _make_state(
        budget={"max_worker_calls": 1},
        constraints={"task_type": "scout"},
        attempt_count=1,
        result=None,
        timeline_events=[],
    )
    ledger = compute_hierarchical_budget_ledger(state, active_slice="primary")
    assert ledger.worker_calls_used == 0
    assert ledger.remaining_worker_calls == 1

    eligible, reason, gate_ledger = check_pre_dispatch_budget_gate(state, active_slice="primary")
    assert eligible
    assert reason is None
    assert gate_ledger.remaining_worker_calls == 1


def test_decompose_task_wires_max_node_attempts_budget():
    from orchestrator.graph import decompose_task
    from orchestrator.state import TaskPlan, TaskPlanStep

    state = _make_state(
        budget={"max_node_attempts": 1},
    )
    state = state.model_copy(
        update={
            "task_plan": TaskPlan(
                triggered=True,
                complexity_reason="multi_file_task",
                steps=[
                    TaskPlanStep(
                        step_id="step-1",
                        title="Step 1",
                        expected_outcome="Desc 1",
                        node_kind="inspect",
                        execution_mode="read_only",
                        aggregation_role="context",
                    ),
                    TaskPlanStep(
                        step_id="step-2",
                        title="Step 2",
                        expected_outcome="Desc 2",
                        node_kind="implement",
                        execution_mode="mutable",
                        aggregation_role="mutation",
                    ),
                ],
            ),
            "task_spec": TaskSpec(
                task_type="feature",
                goal="Goal",
                acceptance_criteria=["AC1"],
                verification_commands=["pytest"],
            ),
        }
    )
    res = decompose_task(state)
    assert res["decomposed_plan"]["status"] == "decomposed"
    for node in res["decomposed_plan"]["nodes"]:
        assert node["max_attempts"] == 1


def test_decomposed_node_accumulation_exhausts_budget_gate():
    """Sequential DAG node accumulation should increment calls in pre-dispatch gate."""
    state = _make_state(
        budget={"max_worker_calls": 2, "max_task_wall_time_seconds": 600},
    )
    # Node 1 finishes
    outcome_1 = NodeOutcome(
        node_id="node_1",
        status="completed",
        result=WorkerResult(status="success", budget_usage={"wall_clock_seconds": 30.0}),
        attempts=1,
    )
    node_state_1 = state.model_copy(update={"node_outcomes": [outcome_1]})
    eligible_1, reason_1, ledger_1 = check_pre_dispatch_budget_gate(
        node_state_1, active_slice="node"
    )
    assert eligible_1
    assert reason_1 is None
    assert ledger_1.worker_calls_used == 1
    assert ledger_1.wall_clock_seconds_used == 30.0
    assert ledger_1.remaining_worker_calls == 1

    # Node 2 finishes (2 calls used out of 2)
    outcome_2 = NodeOutcome(
        node_id="node_2",
        status="completed",
        result=WorkerResult(status="success", budget_usage={"wall_clock_seconds": 40.0}),
        attempts=1,
    )
    node_state_2 = state.model_copy(update={"node_outcomes": [outcome_1, outcome_2]})
    eligible_2, reason_2, ledger_2 = check_pre_dispatch_budget_gate(
        node_state_2, active_slice="node"
    )
    assert not eligible_2
    assert reason_2 == "max_worker_calls"
    assert ledger_2.worker_calls_used == 2
    assert ledger_2.wall_clock_seconds_used == 70.0
    assert ledger_2.remaining_worker_calls == 0


def test_evaluate_hierarchical_budget_compliance_clean_and_violations():
    spec = resolve_budget_spec(
        {
            "max_task_wall_time_seconds": 500,
            "max_worker_calls": 3,
            "max_concurrent_nodes": 2,
            "max_node_attempts": 2,
            "max_repair_passes": 1,
        }
    )

    clean_usage = {
        "hierarchical_budget": {
            "wall_clock_seconds_used": 400.0,
            "worker_calls_used": 2,
            "repair_passes_used": 1,
            "verifier_repair_passes_used": 1,
            "review_repair_passes_used": 0,
        }
    }
    report_clean = evaluate_hierarchical_budget_compliance(
        spec, clean_usage, observed_max_concurrency=2, observed_node_attempts={"n1": 2}
    )
    assert report_clean.compliant
    assert len(report_clean.violations) == 0

    violated_usage = {
        "hierarchical_budget": {
            "wall_clock_seconds_used": 550.0,
            "worker_calls_used": 4,
            "repair_passes_used": 2,
            "verifier_repair_passes_used": 2,
            "review_repair_passes_used": 0,
        }
    }
    report_violated = evaluate_hierarchical_budget_compliance(
        spec,
        violated_usage,
        observed_max_concurrency=3,
        observed_node_attempts={"n1": 3},
    )
    assert not report_violated.compliant
    assert not report_violated.task_wall_time_compliant
    assert not report_violated.worker_calls_compliant
    assert not report_violated.node_attempts_compliant
    assert not report_violated.repair_passes_compliant
    assert not report_violated.concurrency_compliant
    assert len(report_violated.violations) == 5
