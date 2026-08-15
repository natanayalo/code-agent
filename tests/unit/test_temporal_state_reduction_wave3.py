"""Unit and behavioral equivalence tests for M28.5B Wave 3A state reduction.

Covers the plan fields pruned from intermediate TemporalTaskState snapshots:
- task_plan (exact timeline rehydration with None vs [] dependency preservation)
- decomposed_plan (exact timeline rehydration with operational relational validation)
- node_outcomes (strictly preserved in snapshot during Wave 3A)
"""

from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.base import Base, utc_now
from db.enums import (
    ExecutionPlanNodeStatus,
    HumanInteractionType,
    TimelineEventType,
)
from db.models import HumanInteraction, Task
from orchestrator.decomposition import decompose_task_plan
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.execution_resume_service import (
    restore_decomposed_plan_from_events,
    restore_task_plan_from_events,
    validate_decomposed_plan_projection,
)
from orchestrator.state import (
    ApprovalCheckpoint,
    DecomposedTaskNode,
    DecomposedTaskPlan,
    NodeOutcome,
    OrchestratorState,
    SessionRef,
    TaskPlan,
    TaskPlanStep,
    TaskRequest,
    TaskSpec,
    TaskTimelineEventState,
)
from orchestrator.temporal.activities import (
    EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS,
    TaskExecutionActivities,
    _rehydrate_dag_state,
    _resolve_permission_escalation_state,
    _serialize_temporal_task_state,
)
from repositories import (
    ExecutionPlanRepository,
    TaskRepository,
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    session_scope,
)
from workers import WorkerResult


def _make_db() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_activities(factory: sessionmaker[Session]) -> TaskExecutionActivities:
    svc = MagicMock()
    svc.worker = MagicMock()
    svc.worker_profiles = {}
    svc.enable_worker_profiles = False
    svc.enable_independent_verifier = False
    svc.session_factory = factory
    svc.workspace_manager = None
    svc.retention_seconds = None
    svc.orchestrator_brain = None
    svc.progress_notifier = None
    svc._persist_execution_outcome = types.MethodType(_persist_execution_outcome, svc)

    async def _async_run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    svc._run_blocking = _async_run_blocking
    return TaskExecutionActivities(svc)


def _make_sample_state(
    task_id: str = "task-w3-1",
    session_id: str = "session-w3-1",
) -> OrchestratorState:
    return OrchestratorState(
        task=TaskRequest(
            task_id=task_id,
            repo_url="https://github.com/example/repo",
            task_text="Run complex refactoring task",
        ),
        session=SessionRef(
            session_id=session_id,
            user_id="user-1",
            channel="api",
            external_thread_id="thread-1",
        ),
        attempt_count=0,
    )


def _seed_task_and_timeline(
    factory: sessionmaker[Session],
    task_id: str,
    *,
    task_plan: TaskPlan | None = None,
    decomposed_plan: DecomposedTaskPlan | None = None,
    constraints: dict[str, Any] | None = None,
) -> None:
    with session_scope(factory) as session:
        session.add(
            Task(
                id=task_id,
                session_id="session-1",
                repo_url="https://github.com/example/repo",
                task_text="Task description",
                constraints=constraints or {},
            )
        )
        if task_plan is not None:
            TaskTimelineRepository(session).create_next_for_attempt(
                task_id=task_id,
                attempt_number=0,
                event_type=TimelineEventType.TASK_PLANNED,
                message="Plan generated",
                payload={"planning": "generated", **task_plan.model_dump(mode="json")},
            )
        if decomposed_plan is not None:
            TaskTimelineRepository(session).create_next_for_attempt(
                task_id=task_id,
                attempt_number=0,
                event_type=TimelineEventType.TASK_PLANNED,
                message="Decomposed",
                payload={"decomposition": decomposed_plan.model_dump(mode="json")},
            )


def _seed_sql_plan_nodes(
    session: Session,
    task_id: str,
    nodes: list[DecomposedTaskNode],
) -> Any:
    plan = ExecutionPlanRepository(session).create(task_id=task_id)
    for seq, node in enumerate(nodes):
        ExecutionPlanRepository(session).add_node(
            plan_id=plan.id,
            node_id=node.node_id,
            goal=node.title,
            sequence_number=seq,
            depends_on=node.depends_on,
            task_spec=node.task_spec.model_dump(mode="json") if node.task_spec else {},
            node_kind=node.node_kind,
            aggregation_role=node.aggregation_role,
            execution_mode=node.execution_mode,
            parallel_safe=node.parallel_safe,
        )
    return plan


# ---------------------------------------------------------------------------
# Test 1: Field exclusion invariants
# ---------------------------------------------------------------------------


def test_wave3a_field_exclusions_and_serialization() -> None:
    """task_plan and decomposed_plan must be excluded; node_outcomes must be retained."""
    assert "task_plan" in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS
    assert "decomposed_plan" in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS
    assert "node_outcomes" not in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS

    step1 = TaskPlanStep(
        step_id="step-1",
        title="Inspect repo",
        expected_outcome="Analysis complete",
        node_kind="inspect",
        aggregation_role="context",
        execution_mode="read_only",
        parallel_safe=False,
    )
    task_plan = TaskPlan(steps=[step1], complexity_reason="complex_task", triggered=True)
    task_spec = TaskSpec(goal="Inspect repo", acceptance_criteria=["Read only"])
    node1 = DecomposedTaskNode(
        node_id="step-1",
        title="Inspect repo",
        depends_on=[],
        task_spec=task_spec,
        node_kind="inspect",
        aggregation_role="context",
        execution_mode="read_only",
        parallel_safe=False,
    )
    decomposed_plan = DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        reason="complex_task",
        nodes=[node1],
    )
    outcome1 = NodeOutcome(
        node_id="step-1",
        status="completed",
        result=WorkerResult(status="success", summary="Found all files"),
        logical_activity_key="node-wave-key-1",
    )

    state = _make_sample_state()
    state.task_plan = task_plan
    state.decomposed_plan = decomposed_plan
    state.node_outcomes = [outcome1]

    serialized = _serialize_temporal_task_state(state)
    assert "task_plan" not in serialized
    assert "decomposed_plan" not in serialized
    assert "node_outcomes" in serialized
    assert len(serialized["node_outcomes"]) == 1
    assert serialized["node_outcomes"][0]["node_id"] == "step-1"


# ---------------------------------------------------------------------------
# Test 2: TaskPlan exact round-trip (None vs [] dependencies, metadata)
# ---------------------------------------------------------------------------


def test_task_plan_exact_round_trip() -> None:
    """TaskPlan must preserve None vs [] dependencies and planner metadata."""
    step1 = TaskPlanStep(
        step_id="step-1",
        title="Inspect code",
        depends_on=None,
        expected_outcome="Inspected",
        node_kind="inspect",
        aggregation_role="context",
        execution_mode="read_only",
    )
    step2 = TaskPlanStep(
        step_id="step-2",
        title="Implement change",
        depends_on=None,
        expected_outcome="Fixed",
        node_kind="implement",
        aggregation_role="mutation",
    )
    step3 = TaskPlanStep(
        step_id="step-3",
        title="Independent verification",
        depends_on=[],
        expected_outcome="Verified",
        node_kind="verify",
        aggregation_role="validation",
    )
    original_plan = TaskPlan(
        steps=[step1, step2, step3],
        complexity_reason="complex_refactoring",
        triggered=True,
    )

    event = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_PLANNED.value,
        attempt_number=0,
        sequence_number=1,
        message="Created structured plan",
        payload={"planning": "generated", **original_plan.model_dump(mode="json")},
        created_at=utc_now(),
    )

    restored = restore_task_plan_from_events([event])
    assert restored is not None
    assert restored.complexity_reason == "complex_refactoring"
    assert restored.triggered is True
    assert len(restored.steps) == 3
    assert restored.steps[0].depends_on is None
    assert restored.steps[1].depends_on is None
    assert restored.steps[2].depends_on == []

    parent_spec = TaskSpec(goal="Refactor system", acceptance_criteria=["Tests pass"])
    decomp_orig = decompose_task_plan(original_plan, parent_spec)
    decomp_rest = decompose_task_plan(restored, parent_spec)
    assert decomp_orig.model_dump(mode="json") == decomp_rest.model_dump(mode="json")
    assert decomp_rest.nodes[1].depends_on == ["step-1"]
    assert decomp_rest.nodes[2].depends_on == []


# ---------------------------------------------------------------------------
# Test 3: Pre-decomposition round trip
# ---------------------------------------------------------------------------


def test_pre_decomposition_round_trip() -> None:
    """classify_and_plan -> persist pruned snapshot -> reload -> decompose_task DAG."""
    factory = _make_db()
    task_id = "task-pre-decomp-1"

    original_plan = TaskPlan(
        steps=[
            TaskPlanStep(
                step_id="step-1",
                title="Inspect",
                depends_on=None,
                expected_outcome="Inspected",
                node_kind="inspect",
                aggregation_role="context",
                execution_mode="read_only",
            ),
            TaskPlanStep(
                step_id="step-2",
                title="Modify",
                depends_on=None,
                expected_outcome="Modified",
                node_kind="implement",
                aggregation_role="mutation",
            ),
        ],
        complexity_reason="complex_task",
        triggered=True,
    )
    _seed_task_and_timeline(factory, task_id, task_plan=original_plan)

    with session_scope(factory) as session:
        state = _make_sample_state(task_id=task_id)
        state.task_spec = TaskSpec(goal="Modify code", acceptance_criteria=["Done"])
        state.task_plan = original_plan
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=_serialize_temporal_task_state(state)
        )

    activities = _make_activities(factory)
    loaded_state = activities._get_current_state(task_id)
    assert loaded_state.task_plan is not None
    assert loaded_state.task_plan.complexity_reason == "complex_task"
    assert loaded_state.decomposed_plan is None

    decomp = decompose_task_plan(loaded_state.task_plan, loaded_state.task_spec)
    assert decomp.status == "decomposed"
    assert len(decomp.nodes) == 2


# ---------------------------------------------------------------------------
# Test 4: Initial approval before decomposition (approval=True and approval=False)
# ---------------------------------------------------------------------------


def test_initial_approval_before_decomposition_branches() -> None:
    """Awaiting approval before decomposition succeeds on approval and rejection."""
    factory = _make_db()
    task_id_app = "task-init-app-1"
    task_id_rej = "task-init-rej-1"

    task_plan = TaskPlan(
        steps=[
            TaskPlanStep(
                step_id="step-1",
                title="Plan step",
                depends_on=[],
                expected_outcome="Done",
                node_kind="implement",
                aggregation_role="mutation",
            )
        ],
        complexity_reason="requires_approval",
        triggered=True,
    )

    for task_id in [task_id_app, task_id_rej]:
        _seed_task_and_timeline(
            factory,
            task_id,
            task_plan=task_plan,
            constraints={"approval": {"status": "pending"}},
        )
        with session_scope(factory) as session:
            plan = ExecutionPlanRepository(session).create(task_id=task_id)
            ExecutionPlanRepository(session).add_node(
                plan_id=plan.id,
                node_id="step-1",
                goal="Plan step",
                sequence_number=0,
                depends_on=[],
            )
            state = _make_sample_state(task_id=task_id)
            state.task_plan = task_plan
            state.approval = ApprovalCheckpoint(required=True, status="pending")
            TemporalTaskStateRepository(session).upsert(
                task_id=task_id, state=_serialize_temporal_task_state(state)
            )

    activities = _make_activities(factory)

    # Branch 1: Approved -> reloads task_plan and leaves decomposed_plan None
    with session_scope(factory) as session:
        t_app = TaskRepository(session).get(task_id_app)
        assert t_app is not None
        t_app.constraints = {"approval": {"status": "approved"}}

    loaded_app = activities._get_current_state(task_id_app)
    assert loaded_app.approval.status == "approved"
    assert loaded_app.task_plan is not None
    assert loaded_app.decomposed_plan is None

    # Branch 2: Rejected -> persist_rejected_session_state reads pruned snapshot without error
    with session_scope(factory) as session:
        t_rej = TaskRepository(session).get(task_id_rej)
        assert t_rej is not None
        t_rej.constraints = {"approval": {"status": "rejected"}}

    asyncio.run(activities.persist_rejected_session_state(task_id_rej))
    with session_scope(factory) as session:
        assert TemporalTaskStateRepository(session).get(task_id=task_id_rej) is None


# ---------------------------------------------------------------------------
# Test 5: Decomposition fallback round trip
# ---------------------------------------------------------------------------


def test_decomposition_fallback_round_trip() -> None:
    """Fallback DecomposedTaskPlan survives pruned snapshot and workflow remains monolithic."""
    factory = _make_db()
    task_id = "task-fallback-1"
    fallback_plan = DecomposedTaskPlan(
        triggered=False,
        status="fallback",
        reason="task_plan_has_no_steps",
        nodes=[],
    )
    _seed_task_and_timeline(factory, task_id, decomposed_plan=fallback_plan)

    with session_scope(factory) as session:
        state = _make_sample_state(task_id=task_id)
        state.decomposed_plan = fallback_plan
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=_serialize_temporal_task_state(state)
        )

    activities = _make_activities(factory)
    loaded = activities._get_current_state(task_id)
    assert loaded.decomposed_plan is not None
    assert loaded.decomposed_plan.status == "fallback"
    assert loaded.decomposed_plan.reason == "task_plan_has_no_steps"
    assert len(loaded.decomposed_plan.nodes) == 0


# ---------------------------------------------------------------------------
# Test 6: Read-only plan round trip and fan-out metadata preservation
# ---------------------------------------------------------------------------


def test_read_only_plan_round_trip_and_fanout() -> None:
    """Read-only plan survives with parallel_safe and execution_mode intact."""
    factory = _make_db()
    task_id = "task-readonly-1"

    task_spec = TaskSpec(goal="Read only scan", task_type="scout", allowed_actions=[])
    node1 = DecomposedTaskNode(
        node_id="scan-1",
        title="Inspect frontend",
        depends_on=[],
        task_spec=task_spec,
        node_kind="inspect",
        aggregation_role="context",
        execution_mode="read_only",
        parallel_safe=True,
    )
    node2 = DecomposedTaskNode(
        node_id="scan-2",
        title="Inspect backend",
        depends_on=[],
        task_spec=task_spec,
        node_kind="inspect",
        aggregation_role="context",
        execution_mode="read_only",
        parallel_safe=True,
    )
    decomposed_plan = DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        reason="read_only_fanout",
        nodes=[node1, node2],
    )
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        _seed_sql_plan_nodes(session, task_id, [node1, node2])
        state = _make_sample_state(task_id=task_id)
        state.decomposed_plan = decomposed_plan
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=_serialize_temporal_task_state(state)
        )

    activities = _make_activities(factory)
    loaded = activities._get_current_state(task_id)
    assert loaded.decomposed_plan is not None
    assert loaded.decomposed_plan.status == "decomposed"
    assert len(loaded.decomposed_plan.nodes) == 2
    assert loaded.decomposed_plan.nodes[0].parallel_safe is True
    assert loaded.decomposed_plan.nodes[0].execution_mode == "read_only"
    assert loaded.decomposed_plan.nodes[1].parallel_safe is True


# ---------------------------------------------------------------------------
# Test 7: Crash-gap preservation (node_outcomes invariant)
# ---------------------------------------------------------------------------


def test_crash_gap_preservation_node_outcomes_invariant() -> None:
    """Terminal evidence in Postgres must NOT be marked parent-merged during rehydration."""
    factory = _make_db()
    task_id = "task-crash-gap-1"

    node1 = DecomposedTaskNode(
        node_id="step-1",
        title="Execute node",
        depends_on=[],
        task_spec=TaskSpec(goal="Run", acceptance_criteria=[]),
        node_kind="implement",
        aggregation_role="mutation",
        execution_mode="mutable",
        parallel_safe=False,
    )
    decomposed_plan = DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        nodes=[node1],
    )
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node1])
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id=node1.node_id,
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key="activity-key-step-1",
            terminal_result_digest="digest-123",
            terminal_result_payload={"node_outcome": {"status": "completed"}},
        )
        state = _make_sample_state(task_id=task_id)
        state.decomposed_plan = decomposed_plan
        state.node_outcomes = []
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=_serialize_temporal_task_state(state)
        )

    activities = _make_activities(factory)
    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is not None
        loaded_state = OrchestratorState.model_validate(snapshot.state)
        before_outcomes = list(loaded_state.node_outcomes)

        rehydrated = _rehydrate_dag_state(session, task_id, loaded_state, raw_snapshot=snapshot)
        assert rehydrated.node_outcomes == before_outcomes
        assert len(rehydrated.node_outcomes) == 0

    selection = asyncio.run(activities._select_next_node(task_id, fanout_contract_enabled=False))
    assert selection["action"] == "merge_terminal"
    assert selection["node_id"] == "step-1"
    assert selection["logical_activity_key"] == "activity-key-step-1"


# ---------------------------------------------------------------------------
# Test 8: Permission escalation across reader boundaries
# ---------------------------------------------------------------------------


def test_permission_escalation_across_reader_boundaries() -> None:
    """Blocked node in pruned snapshot correctly triggers and resolves permission escalation."""
    factory = _make_db()
    task_id = "task-perm-esc-1"

    node1 = DecomposedTaskNode(
        node_id="step-1",
        title="Blocked step",
        depends_on=[],
        task_spec=TaskSpec(goal="Run sensitive command", acceptance_criteria=[]),
        node_kind="implement",
        aggregation_role="mutation",
        execution_mode="mutable",
        parallel_safe=False,
    )
    decomposed_plan = DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        nodes=[node1],
    )
    blocked_outcome = NodeOutcome(
        node_id="step-1",
        status="blocked",
        result=WorkerResult(
            status="failure",
            summary="Permission denied",
            next_action_hint="request_higher_permission",
            requested_permission="danger-full-access",
        ),
        attempts=1,
    )
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node1])
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id="step-1",
            status=ExecutionPlanNodeStatus.BLOCKED,
        )
        state = _make_sample_state(task_id=task_id)
        state.decomposed_plan = decomposed_plan
        state.node_outcomes = [blocked_outcome]
        state.result = blocked_outcome.result
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=_serialize_temporal_task_state(state)
        )

    activities = _make_activities(factory)
    asyncio.run(activities.request_permission_escalation(task_id))
    with session_scope(factory) as session:
        interaction = (
            session.query(HumanInteraction)
            .filter_by(task_id=task_id, interaction_type=HumanInteractionType.PERMISSION)
            .one_or_none()
        )
        assert interaction is not None
        assert interaction.data.get("requested_permission") == "danger-full-access"

    _resolve_permission_escalation_state(factory, task_id, approved=True)
    with session_scope(factory) as session:
        node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert node is not None
        assert node.status == ExecutionPlanNodeStatus.PENDING


# ---------------------------------------------------------------------------
# Test 9: Fail-closed validation on malformed events or projection mismatch
# ---------------------------------------------------------------------------


def test_fail_closed_validation() -> None:
    """Malformed timeline events and execution plan projection mismatches must fail closed."""
    malformed_task_event = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_PLANNED.value,
        attempt_number=0,
        sequence_number=1,
        payload={"steps": "not-a-list"},
    )
    with pytest.raises(RuntimeError, match="Malformed TASK_PLANNED steps payload"):
        restore_task_plan_from_events([malformed_task_event])

    malformed_decomp_event = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_PLANNED.value,
        attempt_number=0,
        sequence_number=2,
        payload={"decomposition": "not-a-dict"},
    )
    with pytest.raises(RuntimeError, match="Malformed TASK_PLANNED decomposition payload"):
        restore_decomposed_plan_from_events([malformed_decomp_event])

    node1 = DecomposedTaskNode(
        node_id="step-1",
        title="Node 1",
        depends_on=[],
        task_spec=TaskSpec(goal="N1", acceptance_criteria=[]),
        node_kind="implement",
        aggregation_role="mutation",
        execution_mode="mutable",
        parallel_safe=False,
    )
    plan_model = DecomposedTaskPlan(status="decomposed", nodes=[node1])
    sql_plan_mock = MagicMock()
    mock_node = lambda nid, g: MagicMock(  # noqa: E731
        node_id=nid,
        goal=g,
        sequence_number=0,
        depends_on=[],
        task_spec={},
        node_kind="implement",
        aggregation_role="mutation",
        execution_mode="mutable",
        parallel_safe=False,
    )
    sql_plan_mock.nodes = [mock_node("s1", "N1"), mock_node("s2", "N2")]
    with pytest.raises(RuntimeError, match="ExecutionPlan node count"):
        validate_decomposed_plan_projection(sql_plan_mock, plan_model)


# ---------------------------------------------------------------------------
# Test 10: Legacy rolling snapshot compatibility
# ---------------------------------------------------------------------------


def test_legacy_rolling_snapshot_compatibility() -> None:
    """Legacy snapshots containing serialized plans load cleanly as fallback."""
    factory = _make_db()
    task_id = "task-legacy-1"

    task_plan = TaskPlan(
        steps=[
            TaskPlanStep(
                step_id="s1",
                title="Legacy step",
                expected_outcome="o",
                node_kind="implement",
                aggregation_role="mutation",
            )
        ],
        complexity_reason="legacy",
        triggered=True,
    )
    node1 = DecomposedTaskNode(
        node_id="s1",
        title="Legacy step",
        depends_on=[],
        task_spec=TaskSpec(goal="Legacy", acceptance_criteria=[]),
        node_kind="implement",
        aggregation_role="mutation",
        execution_mode="mutable",
        parallel_safe=False,
    )
    decomposed_plan = DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        nodes=[node1],
    )
    _seed_task_and_timeline(factory, task_id)

    with session_scope(factory) as session:
        _seed_sql_plan_nodes(session, task_id, [node1])
        raw_state_dict = {
            "task": {
                "task_id": task_id,
                "repo_url": "https://github.com/example/repo",
                "task_text": "t",
            },
            "task_plan": task_plan.model_dump(mode="json"),
            "decomposed_plan": decomposed_plan.model_dump(mode="json"),
            "node_outcomes": [],
        }
        TemporalTaskStateRepository(session).upsert(task_id=task_id, state=raw_state_dict)

    activities = _make_activities(factory)
    loaded = activities._get_current_state(task_id)
    assert loaded.task_plan is not None
    assert loaded.task_plan.complexity_reason == "legacy"
    assert loaded.decomposed_plan is not None
    assert loaded.decomposed_plan.status == "decomposed"
    assert len(loaded.decomposed_plan.nodes) == 1
