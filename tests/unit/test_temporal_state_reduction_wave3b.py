"""Unit and behavioral equivalence tests for M28.5B Wave 3B state reduction.

Covers relational merge marker authority, dual writes, rehydration, and node_outcomes pruning.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from db.enums import (
    ExecutionPlanNodeStatus,
)
from orchestrator.node_execution import (
    NodeActivityRequest,
    NodeActivityResultRef,
    logical_activity_key,
)
from orchestrator.state import (
    DecomposedTaskPlan,
    NodeOutcome,
)
from orchestrator.temporal.activities import (
    EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS,
    _resolve_permission_escalation_state,
    _serialize_temporal_task_state,
)
from orchestrator.temporal.node_wave import NodeWaveItem, NodeWaveSelectionV2
from repositories import (
    ExecutionPlanRepository,
    TemporalTaskStateRepository,
    session_scope,
)
from tests.unit.wave3b_test_helpers import (
    add_attempt as _add_attempt,
)
from tests.unit.wave3b_test_helpers import (
    make_activities as _make_activities,
)
from tests.unit.wave3b_test_helpers import (
    make_db as _make_db,
)
from tests.unit.wave3b_test_helpers import (
    make_outcome_payload as _make_outcome_payload,
)
from tests.unit.wave3b_test_helpers import (
    make_sample_node as _make_sample_node,
)
from tests.unit.wave3b_test_helpers import (
    make_sample_state as _make_sample_state,
)
from tests.unit.wave3b_test_helpers import (
    seed_snapshot as _seed_snapshot,
)
from tests.unit.wave3b_test_helpers import (
    seed_sql_plan_nodes as _seed_sql_plan_nodes,
)
from tests.unit.wave3b_test_helpers import (
    seed_task_and_timeline as _seed_task_and_timeline,
)
from workers import WorkerResult


def test_wave3b_field_exclusion_and_serialization() -> None:
    """node_outcomes is serialized in Wave 3B.1 snapshots for rolling deployment safety."""
    assert "node_outcomes" not in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS
    outcome = NodeOutcome(
        node_id="step-1",
        status="completed",
        result=WorkerResult(status="success", summary="Done"),
        logical_activity_key="k1",
    )
    state = _make_sample_state()
    state.node_outcomes = [outcome]
    serialized = _serialize_temporal_task_state(state)
    assert "node_outcomes" in serialized


def test_1_k1_merged_k2_unmerged_rehydration_and_selector() -> None:
    """1. K1 merged -> K2 unmerged -> rehydrate returns K1; selector returns merge_terminal(K2)."""
    factory = _make_db()
    task_id = "task-3b-1"
    node = _make_sample_node("step-1", "Execute step")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        k1, k2 = "activity:step-1:1", "activity:step-1:2"
        _, p1, d1 = _make_outcome_payload(
            "step-1", "failed", WorkerResult(status="failure", summary="Attempt 1 failed"), 1, k1
        )
        _, p2, d2 = _make_outcome_payload(
            "step-1", "completed", WorkerResult(status="success", summary="Attempt 2 ok"), 2, k2
        )
        _add_attempt(session, plan.nodes[0].id, 1, k1, "failed", p1, d1)
        _add_attempt(session, plan.nodes[0].id, 2, k2, "completed", p2, d2)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key=k2,
            merged_logical_activity_key=k1,
            terminal_result_digest=d2,
            terminal_result_payload=p2,
        )
        _seed_snapshot(session, task_id, decomposed_plan)

    activities = _make_activities(factory)
    loaded_state = activities._get_current_state(task_id)
    assert len(loaded_state.node_outcomes) == 1
    assert loaded_state.node_outcomes[0].logical_activity_key == k1

    selection = asyncio.run(activities._select_next_node(task_id, fanout_contract_enabled=False))
    assert selection["action"] == "merge_terminal"
    assert selection["logical_activity_key"] == k2


def test_2_blocked_k1_merged_permission_approved_k2_retry() -> None:
    """2. Blocked K1 merged -> permission approved -> K2 retry: old outcome not re-merged."""
    factory = _make_db()
    task_id = "task-3b-2"
    node = _make_sample_node("step-1", "Blocked node")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        k1 = "activity:step-1:1"
        res1 = WorkerResult(
            status="failure",
            summary="Permission needed",
            next_action_hint="request_higher_permission",
            requested_permission="danger-full-access",
        )
        _, p1, d1 = _make_outcome_payload("step-1", "blocked", res1, 1, k1)
        _add_attempt(session, plan.nodes[0].id, 1, k1, "blocked", p1, d1)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.BLOCKED,
            latest_logical_activity_key=k1,
            merged_logical_activity_key=k1,
            terminal_result_digest=d1,
            terminal_result_payload=p1,
        )
        _seed_snapshot(session, task_id, decomposed_plan)

    activities = _make_activities(factory)
    _resolve_permission_escalation_state(factory, task_id, approved=True)

    with session_scope(factory) as session:
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert db_node is not None
        assert db_node.status == ExecutionPlanNodeStatus.PENDING
        assert db_node.merged_logical_activity_key == k1

    selection = asyncio.run(activities._select_next_node(task_id, fanout_contract_enabled=False))
    assert selection["action"] == "execute"


def test_3_legacy_wave3a_snapshot_bootstrap_and_reload() -> None:
    """3. Legacy Wave 3A snapshot -> marker bootstrap -> snapshot pruned -> reload identical."""
    factory = _make_db()
    task_id = "task-3b-3"
    node = _make_sample_node("step-1", "Legacy step")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    k1 = "activity:step-1:1"
    out1, p1, d1 = _make_outcome_payload(
        "step-1", "completed", WorkerResult(status="success", summary="Legacy completed"), 1, k1
    )

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        _add_attempt(session, plan.nodes[0].id, 1, k1, "completed", p1, d1)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key=k1,
            merged_logical_activity_key=None,
            terminal_result_digest=d1,
            terminal_result_payload=p1,
        )
        raw_state_dict = {
            "task": {
                "task_id": task_id,
                "repo_url": "https://github.com/example/repo",
                "task_text": "t",
            },
            "decomposed_plan": decomposed_plan.model_dump(mode="json"),
            "node_outcomes": [out1.model_dump(mode="json")],
        }
        _seed_snapshot(session, task_id, raw_dict=raw_state_dict)

    activities = _make_activities(factory)
    loaded = activities._get_current_state(task_id)

    with session_scope(factory) as session:
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert db_node is not None
        assert db_node.merged_logical_activity_key == k1

    assert len(loaded.node_outcomes) == 1
    assert loaded.node_outcomes[0].logical_activity_key == k1


def test_4_terminal_unmerged_legacy_node_not_backfilled() -> None:
    """4. Terminal-but-unmerged legacy node must not be backfilled as merged."""
    factory = _make_db()
    task_id = "task-3b-4"
    node = _make_sample_node("step-1", "Crash gap node")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    k1 = "activity:step-1:1"
    _, p1, d1 = _make_outcome_payload(
        "step-1",
        "completed",
        WorkerResult(status="success", summary="Finished before crash"),
        1,
        k1,
    )

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        _add_attempt(session, plan.nodes[0].id, 1, k1, "completed", p1, d1)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key=k1,
            merged_logical_activity_key=None,
            terminal_result_digest=d1,
            terminal_result_payload=p1,
        )
        raw_state_dict = {
            "task": {
                "task_id": task_id,
                "repo_url": "https://github.com/example/repo",
                "task_text": "t",
            },
            "decomposed_plan": decomposed_plan.model_dump(mode="json"),
            "node_outcomes": [],
        }
        _seed_snapshot(session, task_id, raw_dict=raw_state_dict)

    activities = _make_activities(factory)
    loaded = activities._get_current_state(task_id)
    assert len(loaded.node_outcomes) == 0

    selection = asyncio.run(activities._select_next_node(task_id, fanout_contract_enabled=False))
    assert selection["action"] == "merge_terminal"
    assert selection["logical_activity_key"] == k1


def test_5_skip_outcome_exact_relational_round_trip() -> None:
    """5. Skip outcome exact relational round-trip."""
    factory = _make_db()
    task_id = "task-3b-5"
    node1 = _make_sample_node("step-1", "Failed step")
    node2 = _make_sample_node("step-2", "Dependent step").model_copy(
        update={"depends_on": ["step-1"]}
    )
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node1, node2])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    k1 = "activity:step-1:1"
    _, p1, d1 = _make_outcome_payload(
        "step-1", "failed", WorkerResult(status="failure", summary="Step 1 failed"), 1, k1
    )

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node1, node2])
        _add_attempt(session, plan.nodes[0].id, 1, k1, "failed", p1, d1)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.FAILED,
            latest_logical_activity_key=k1,
            merged_logical_activity_key=k1,
            terminal_result_digest=d1,
            terminal_result_payload=p1,
        )
        _seed_snapshot(session, task_id, decomposed_plan)

    activities = _make_activities(factory)
    selection = asyncio.run(activities._select_next_node(task_id, fanout_contract_enabled=False))
    assert selection["action"] == "skip"
    assert selection["node_id"] == "step-2"

    merge_res = asyncio.run(
        activities.merge_node_wave(task_id, {"selection": selection, "result_ref": None})
    )
    assert merge_res["continuation"] == "continue"

    with session_scope(factory) as session:
        db_node2 = ExecutionPlanRepository(session).get_node(plan.id, "step-2")
        assert db_node2 is not None
        assert db_node2.status == ExecutionPlanNodeStatus.SKIPPED
        assert db_node2.merged_logical_activity_key == db_node2.latest_logical_activity_key

    loaded = activities._get_current_state(task_id)
    skip_outcome = next((o for o in loaded.node_outcomes if o.node_id == "step-2"), None)
    assert skip_outcome is not None
    assert skip_outcome.status == "skipped"


def test_6_fanout_missing_evidence_synthetic_failure_round_trip() -> None:
    """6. Fan-out missing-evidence synthetic failure exact relational round-trip."""
    factory = _make_db()
    task_id = "task-3b-6"
    node1 = _make_sample_node("node-1", "Node 1", mode="read_only", parallel_safe=True)
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node1])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node1])
        _seed_snapshot(session, task_id, decomposed_plan)

    key = logical_activity_key(str(plan.id), node1.node_id, 1)
    act_req = NodeActivityRequest(
        task_id=task_id,
        plan_id=str(plan.id),
        node_id=node1.node_id,
        logical_attempt=1,
        logical_activity_key=key,
        effective_input_digest="a" * 64,
    )
    selection_data = NodeWaveSelectionV2(
        action="execute_wave",
        wave_id="wave-1",
        items=[
            NodeWaveItem(
                node_id=node1.node_id,
                execution_task_queue="task-queue",
                activity_request=act_req,
            )
        ],
    ).model_dump(mode="json")

    activities = _make_activities(factory)
    merge_result = activities._merge_v2_wave(
        task_id=task_id,
        selection_data=selection_data,
        result_refs=[None],
    )
    assert merge_result["continuation"] == "fail_task"

    with session_scope(factory) as session:
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "node-1")
        assert db_node is not None
        assert db_node.status == ExecutionPlanNodeStatus.FAILED
        assert db_node.failure_kind == "sandbox_infra"
        assert db_node.merged_logical_activity_key == key

    loaded = activities._get_current_state(task_id)
    assert len(loaded.node_outcomes) == 1
    assert loaded.node_outcomes[0].result.failure_kind == "sandbox_infra"


def test_7_marker_nonexistent_attempt_fails_closed() -> None:
    """7. Marker referring to nonexistent attempt/payload -> RuntimeError (fail-closed)."""
    factory = _make_db()
    task_id = "task-3b-7"
    node = _make_sample_node("step-1", "Corrupt marker node")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            merged_logical_activity_key="ghost-activity-key",
            latest_logical_activity_key=None,
            terminal_result_payload=None,
        )
        _seed_snapshot(session, task_id, decomposed_plan)

    activities = _make_activities(factory)
    with pytest.raises(RuntimeError, match="ghost-activity-key"):
        activities._get_current_state(task_id)


def test_8_crash_after_marker_commit_converges_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """8. Crash after marker commit before snapshot persistence -> retry converges."""
    factory = _make_db()
    task_id = "task-3b-8"
    node = _make_sample_node("step-1", "Step 1")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        k1 = logical_activity_key(str(plan.id), "step-1", 1)
        _, p1, d1 = _make_outcome_payload(
            "step-1", "completed", WorkerResult(status="success", summary="Done 1"), 1, k1
        )
        _add_attempt(session, plan.nodes[0].id, 1, k1, "completed", p1, d1)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key=k1,
            merged_logical_activity_key=None,
            terminal_result_digest=d1,
            terminal_result_payload=p1,
        )
        _seed_snapshot(session, task_id, decomposed_plan)

    activities = _make_activities(factory)
    selection = {
        "action": "merge_terminal",
        "node_id": "step-1",
        "logical_activity_key": k1,
    }
    merge_req = {"selection": selection, "result_ref": None}

    calls = []
    real_persist = activities._persist_intermediate_state

    def crashing_persist(*args: Any, **kwargs: Any) -> None:
        if not calls:
            calls.append(1)
            raise RuntimeError("Simulated crash right before snapshot upsert")
        real_persist(*args, **kwargs)

    monkeypatch.setattr(activities, "_persist_intermediate_state", crashing_persist)

    with pytest.raises(RuntimeError, match="Simulated crash right before snapshot upsert"):
        asyncio.run(activities.merge_node_wave(task_id, merge_req))

    with session_scope(factory) as session:
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert db_node is not None
        assert db_node.merged_logical_activity_key == k1

    retry_result = asyncio.run(activities.merge_node_wave(task_id, merge_req))
    assert retry_result["continuation"] == "continue"

    loaded = activities._get_current_state(task_id)
    assert len(loaded.node_outcomes) == 1
    assert loaded.node_outcomes[0].logical_activity_key == k1


def test_9_v2_sibling_terminal_before_other_sibling_failure_markers() -> None:
    """9. V2 sibling terminal-before-other-sibling-failure maintains correct markers."""
    factory = _make_db()
    task_id = "task-3b-9"
    node1 = _make_sample_node("n1", "Node 1", mode="read_only", parallel_safe=True)
    node2 = _make_sample_node("n2", "Node 2", mode="read_only", parallel_safe=True)
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node1, node2])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node1, node2])
        k1 = logical_activity_key(str(plan.id), "n1", 1)
        k2 = logical_activity_key(str(plan.id), "n2", 1)
        _, p1, d1 = _make_outcome_payload(
            "n1", "completed", WorkerResult(status="success", summary="Node 1 ok"), 1, k1
        )
        _add_attempt(session, plan.nodes[0].id, 1, k1, "completed", p1, d1)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="n1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key=k1,
            terminal_result_digest=d1,
            terminal_result_payload=p1,
        )
        _seed_snapshot(session, task_id, decomposed_plan)

    act_req1 = NodeActivityRequest(
        task_id=task_id,
        plan_id=str(plan.id),
        node_id="n1",
        logical_attempt=1,
        logical_activity_key=k1,
        effective_input_digest="a" * 64,
    )
    act_req2 = NodeActivityRequest(
        task_id=task_id,
        plan_id=str(plan.id),
        node_id="n2",
        logical_attempt=1,
        logical_activity_key=k2,
        effective_input_digest="b" * 64,
    )
    selection_data = NodeWaveSelectionV2(
        action="execute_wave",
        wave_id="wave-1",
        items=[
            NodeWaveItem(node_id="n1", execution_task_queue="q", activity_request=act_req1),
            NodeWaveItem(node_id="n2", execution_task_queue="q", activity_request=act_req2),
        ],
    ).model_dump(mode="json")
    result_refs = [
        NodeActivityResultRef(
            node_id="n1",
            logical_activity_key=k1,
            status="completed",
            result_digest=d1,
            continuation="continue",
        ).model_dump(mode="json"),
        None,
    ]

    activities = _make_activities(factory)
    activities._merge_v2_wave(
        task_id=task_id, selection_data=selection_data, result_refs=result_refs
    )

    with session_scope(factory) as session:
        db_n1 = ExecutionPlanRepository(session).get_node(plan.id, "n1")
        db_n2 = ExecutionPlanRepository(session).get_node(plan.id, "n2")
        assert db_n1.merged_logical_activity_key == k1
        assert db_n2.merged_logical_activity_key == k2
        assert db_n2.status == ExecutionPlanNodeStatus.FAILED


def test_10_no_snapshot_recovery_uses_marker_confirmed_outcomes_only() -> None:
    """10. No-snapshot recovery uses marker-confirmed outcomes only, not arbitrary nodes."""
    factory = _make_db()
    task_id = "task-3b-10"
    node1 = _make_sample_node("n1", "Node 1")
    node2 = _make_sample_node("n2", "Node 2")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node1, node2])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    k1 = "activity:n1:1"
    _, p1, d1 = _make_outcome_payload(
        "n1", "completed", WorkerResult(status="success", summary="N1 merged"), 1, k1
    )

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node1, node2])
        _add_attempt(session, plan.nodes[0].id, 1, k1, "completed", p1, d1)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="n1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key=k1,
            merged_logical_activity_key=k1,
            terminal_result_digest=d1,
            terminal_result_payload=p1,
        )
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="n2",
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key="activity:n2:1",
            merged_logical_activity_key=None,
            terminal_result_digest="digest-2",
            terminal_result_payload={"node_outcome": {"status": "completed"}},
        )
        assert TemporalTaskStateRepository(session).get(task_id=task_id) is None

    activities = _make_activities(factory)
    loaded = activities._get_current_state(task_id)
    assert len(loaded.node_outcomes) == 1
    assert loaded.node_outcomes[0].node_id == "n1"


def test_11_conflicting_relational_marker_raises_runtime_error() -> None:
    """11. Existing relational marker conflicts with snapshot marker -> RuntimeError."""
    factory = _make_db()
    task_id = "task-3b-11"
    node = _make_sample_node("step-1", "Conflict step")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    snap_outcome = NodeOutcome(
        node_id="step-1",
        status="completed",
        result=WorkerResult(status="success", summary="Done"),
        logical_activity_key="snapshot-marker-key",
    )

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            merged_logical_activity_key="db-marker-key",
        )
        raw_state_dict = {
            "task": {
                "task_id": task_id,
                "repo_url": "https://github.com/example/repo",
                "task_text": "t",
            },
            "decomposed_plan": decomposed_plan.model_dump(mode="json"),
            "node_outcomes": [snap_outcome.model_dump(mode="json")],
        }
        _seed_snapshot(session, task_id, raw_dict=raw_state_dict)

    activities = _make_activities(factory)
    with pytest.raises(RuntimeError, match="Conflicting relational merge marker"):
        activities._get_current_state(task_id)


def test_12_legacy_outcome_without_key_raises_runtime_error() -> None:
    """12. Legacy outcome has no key and cannot be uniquely proven -> RuntimeError."""
    factory = _make_db()
    task_id = "task-3b-12"
    node = _make_sample_node("step-1", "No key step")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    no_key_outcome = NodeOutcome(
        node_id="step-1",
        status="completed",
        result=WorkerResult(status="success", summary="Done"),
        logical_activity_key=None,
    )

    with session_scope(factory) as session:
        _seed_sql_plan_nodes(session, task_id, [node])
        raw_state_dict = {
            "task": {
                "task_id": task_id,
                "repo_url": "https://github.com/example/repo",
                "task_text": "t",
            },
            "decomposed_plan": decomposed_plan.model_dump(mode="json"),
            "node_outcomes": [no_key_outcome.model_dump(mode="json")],
        }
        _seed_snapshot(session, task_id, raw_dict=raw_state_dict)

    activities = _make_activities(factory)
    with pytest.raises(RuntimeError, match="lacks logical_activity_key"):
        activities._get_current_state(task_id)


def test_13_legacy_outcome_payload_parity_mismatch_fails_closed() -> None:
    """13. Legacy snapshot outcome payload conflicts with durable attempt -> fails closed."""
    factory = _make_db()
    task_id = "task-3b-13"
    node = _make_sample_node("step-1", "Step 1")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    k1 = "activity:step-1:1"
    # Durable attempt recorded failure
    _, p1, d1 = _make_outcome_payload(
        "step-1", "failed", WorkerResult(status="failure", summary="Durable failed"), 1, k1
    )
    # Legacy snapshot claimed success
    snap_outcome, _, _ = _make_outcome_payload(
        "step-1", "completed", WorkerResult(status="success", summary="Snapshot success"), 1, k1
    )

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        _add_attempt(session, plan.nodes[0].id, 1, k1, "failed", p1, d1)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.FAILED,
            latest_logical_activity_key=k1,
            merged_logical_activity_key=None,
            terminal_result_digest=d1,
            terminal_result_payload=p1,
        )
        raw_state_dict = {
            "task": {
                "task_id": task_id,
                "repo_url": "https://github.com/example/repo",
                "task_text": "t",
            },
            "decomposed_plan": decomposed_plan.model_dump(mode="json"),
            "node_outcomes": [snap_outcome.model_dump(mode="json")],
        }
        _seed_snapshot(session, task_id, raw_dict=raw_state_dict)

    activities = _make_activities(factory)
    with pytest.raises(RuntimeError, match="conflicts with durable canonical outcome evidence"):
        activities._get_current_state(task_id)

    # Ensure marker was NOT written
    with session_scope(factory) as session:
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert db_node is not None
        assert db_node.merged_logical_activity_key is None


def test_14_blocked_sql_node_without_merged_outcome_fails_closed_in_permission_escalation() -> None:
    """14. Blocked SQL node without marker outcome fails closed on permission escalation."""
    factory = _make_db()
    task_id = "task-3b-14"
    node = _make_sample_node("step-1", "Blocked step")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        # SQL node is BLOCKED, but no attempt / merge marker exists
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.BLOCKED,
            merged_logical_activity_key=None,
        )
        _seed_snapshot(session, task_id, decomposed_plan)

    with pytest.raises(RuntimeError, match="has no marker-confirmed blocked outcome"):
        _resolve_permission_escalation_state(factory, task_id, approved=True)

    with pytest.raises(RuntimeError, match="has no marker-confirmed blocked outcome"):
        _resolve_permission_escalation_state(factory, task_id, approved=False)
