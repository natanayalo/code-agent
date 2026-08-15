"""Parity, legacy bootstrap, and crash recovery tests for M28.5B Wave 3B state reduction.

Covers:
- Sequential retry crash recovery with stale dual-write snapshot (test 11)
- Legacy outcome without key raises RuntimeError (test 12)
- Legacy outcome payload parity mismatch fails closed (test 13)
- Blocked SQL node without marker fails closed in permission escalation (test 14)
- Legacy outcome semantic content parity mismatch fails closed (test 15)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from db.enums import ExecutionPlanNodeStatus
from orchestrator.state import DecomposedTaskPlan, NodeOutcome
from orchestrator.temporal.activities import _resolve_permission_escalation_state
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
    seed_snapshot as _seed_snapshot,
)
from tests.unit.wave3b_test_helpers import (
    seed_sql_plan_nodes as _seed_sql_plan_nodes,
)
from tests.unit.wave3b_test_helpers import (
    seed_task_and_timeline as _seed_task_and_timeline,
)
from workers import WorkerResult


def _seed_sequential_crash_scenario(factory: Any, task_id: str, k1: str, k2: str) -> Any:
    node = _make_sample_node("step-1", "Sequential step")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)
    out1, p1, d1 = _make_outcome_payload(
        "step-1", "failed", WorkerResult(status="failure", summary="Attempt 1 failed"), 1, k1
    )
    _, p2, d2 = _make_outcome_payload(
        "step-1", "completed", WorkerResult(status="success", summary="Attempt 2 succeeded"), 2, k2
    )
    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
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
    return plan


def test_11_sequential_retry_crash_with_stale_snapshot_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11. Snapshot K1 + marker K1 -> merge K2 -> crash after marker -> retry converges to K2."""
    factory = _make_db()
    task_id = "task-3b-11"
    k1, k2 = "activity:step-1:1", "activity:step-1:2"
    plan = _seed_sequential_crash_scenario(factory, task_id, k1, k2)

    activities = _make_activities(factory)
    merge_req = {
        "selection": {
            "action": "merge_terminal",
            "node_id": "step-1",
            "logical_activity_key": k2,
        },
        "result_ref": None,
    }
    calls: list[int] = []
    real_persist = activities._persist_intermediate_state

    def crashing_persist(*args: Any, **kwargs: Any) -> None:
        if not calls:
            calls.append(1)
            raise RuntimeError("Simulated process crash after DB marker commit")
        real_persist(*args, **kwargs)

    monkeypatch.setattr(activities, "_persist_intermediate_state", crashing_persist)

    with pytest.raises(RuntimeError, match="Simulated process crash after DB marker commit"):
        asyncio.run(activities.merge_node_wave(task_id, merge_req))

    with session_scope(factory) as session:
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert db_node is not None
        assert db_node.merged_logical_activity_key == k2
        raw_snap = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert raw_snap is not None
        assert raw_snap.state["node_outcomes"][0]["logical_activity_key"] == k1

    retry_res = asyncio.run(activities.merge_node_wave(task_id, merge_req))
    assert retry_res["continuation"] == "continue"

    with session_scope(factory) as session:
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert db_node is not None
        assert db_node.merged_logical_activity_key == k2
        raw_snap = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert raw_snap is not None
        assert raw_snap.state["node_outcomes"][0]["logical_activity_key"] == k2
        assert raw_snap.state["node_outcomes"][0]["status"] == "completed"


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
    _, p1, d1 = _make_outcome_payload(
        "step-1", "failed", WorkerResult(status="failure", summary="Durable failed"), 1, k1
    )
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


def test_15_legacy_outcome_semantic_content_parity_mismatch_fails_closed() -> None:
    """15. Same status/key but different summary/files without digest fails closed."""
    factory = _make_db()
    task_id = "task-3b-15"
    node = _make_sample_node("step-1", "Step 1")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    k1 = "activity:step-1:1"
    _, p1, d1 = _make_outcome_payload(
        "step-1",
        "completed",
        WorkerResult(
            status="success",
            summary="Durable summary",
            files_changed=["a.py"],
        ),
        1,
        k1,
    )
    snap_outcome = NodeOutcome(
        node_id="step-1",
        status="completed",
        result=WorkerResult(
            status="success",
            summary="Snapshot summary",
            files_changed=["b.py"],
        ),
        attempts=1,
        logical_activity_key=k1,
        result_digest=None,
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
            "node_outcomes": [snap_outcome.model_dump(mode="json")],
        }
        _seed_snapshot(session, task_id, raw_dict=raw_state_dict)

    activities = _make_activities(factory)
    with pytest.raises(RuntimeError, match="conflicts with durable canonical outcome evidence"):
        activities._get_current_state(task_id)

    with session_scope(factory) as session:
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert db_node is not None
        assert db_node.merged_logical_activity_key is None
