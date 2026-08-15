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
from db.models import HumanInteraction, Task, User
from db.models import Session as ConversationSession
from orchestrator.decomposition import decompose_task_plan
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.execution_resume_service import (
    restore_decomposed_plan_from_events,
    restore_task_plan_from_events,
    validate_decomposed_plan_projection,
)
from orchestrator.execution_submission_service import _load_submission_for_task
from orchestrator.node_execution import (
    NodeActivityRequest,
    NodeActivityResultRef,
    _result_digest,
    logical_activity_key,
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
from orchestrator.temporal.node_wave import NodeWaveItem, NodeWaveSelectionV2
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
    svc._load_submission_for_task = types.MethodType(_load_submission_for_task, svc)

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
        if session.query(User).filter_by(id="user-1").first() is None:
            session.add(User(id="user-1", external_user_id="user-1", display_name="Test User"))
        if session.query(ConversationSession).filter_by(id="session-1").first() is None:
            session.add(
                ConversationSession(
                    id="session-1",
                    user_id="user-1",
                    channel="api",
                    external_thread_id="thread-1",
                )
            )
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


def _make_sample_node(
    node_id: str = "step-1",
    title: str = "Node 1",
    mode: str = "mutable",
    parallel_safe: bool = False,
) -> DecomposedTaskNode:
    return DecomposedTaskNode(
        node_id=node_id,
        title=title,
        depends_on=[],
        task_spec=TaskSpec(goal=title, acceptance_criteria=[]),
        node_kind="inspect" if mode == "read_only" else "implement",
        aggregation_role="context" if mode == "read_only" else "mutation",
        execution_mode=mode,
        parallel_safe=parallel_safe,
    )


def _make_step(
    step_id: str,
    title: str = "Step",
    depends_on: list[str] | None = None,
    mode: str = "mutable",
) -> TaskPlanStep:
    return TaskPlanStep(
        step_id=step_id,
        title=title,
        depends_on=depends_on,
        expected_outcome="done",
        node_kind="inspect" if mode == "read_only" else "implement",
        aggregation_role="context" if mode == "read_only" else "mutation",
        execution_mode=mode,
    )


def test_wave3a_field_exclusions_and_serialization() -> None:
    """task_plan, decomposed_plan, and node_outcomes must be excluded."""
    assert "task_plan" in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS
    assert "decomposed_plan" in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS
    assert "node_outcomes" in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS

    step = _make_step("step-1", "Inspect repo", mode="read_only")
    node = _make_sample_node("step-1", "Inspect repo", mode="read_only")
    outcome = NodeOutcome(
        node_id="step-1",
        status="completed",
        result=WorkerResult(status="success", summary="Done"),
    )
    state = _make_sample_state()
    state.task_plan = TaskPlan(steps=[step], triggered=True)
    state.decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    state.node_outcomes = [outcome]

    serialized = _serialize_temporal_task_state(state)
    assert "task_plan" not in serialized
    assert "decomposed_plan" not in serialized
    assert "node_outcomes" not in serialized


def test_task_plan_exact_round_trip() -> None:
    """TaskPlan must preserve None vs [] dependencies and planner metadata."""
    step1 = _make_step("s1", "Inspect", depends_on=None, mode="read_only")
    step2 = _make_step("s2", "Fix", depends_on=None)
    step3 = _make_step("s3", "Verify", depends_on=[])
    plan = TaskPlan(steps=[step1, step2, step3], complexity_reason="cr", triggered=True)
    event = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_PLANNED.value,
        attempt_number=0,
        sequence_number=1,
        message="Planned",
        payload={"planning": "generated", **plan.model_dump(mode="json")},
        created_at=utc_now(),
    )
    restored = restore_task_plan_from_events([event])
    assert restored is not None
    assert restored.complexity_reason == "cr"
    assert restored.steps[0].depends_on is None
    assert restored.steps[2].depends_on == []

    parent_spec = TaskSpec(goal="Goal")
    decomp_orig = decompose_task_plan(plan, parent_spec)
    decomp_rest = decompose_task_plan(restored, parent_spec)
    assert decomp_orig.model_dump(mode="json") == decomp_rest.model_dump(mode="json")
    assert decomp_rest.nodes[1].depends_on == ["s1"]


def test_pre_decomposition_round_trip() -> None:
    """classify_and_plan -> persist pruned snapshot -> reload -> decompose_task DAG."""
    factory = _make_db()
    task_id = "task-pre-decomp-1"
    step = _make_step("s1", "Inspect", mode="read_only")
    plan = TaskPlan(steps=[step], complexity_reason="complex_task", triggered=True)
    _seed_task_and_timeline(factory, task_id, task_plan=plan)

    with session_scope(factory) as session:
        state = _make_sample_state(task_id=task_id)
        state.task_spec = TaskSpec(goal="Modify code")
        state.task_plan = plan
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
    assert len(decomp.nodes) == 1


def test_initial_approval_before_decomposition_branches() -> None:
    """Awaiting approval before decomposition succeeds on approval and rejection."""
    factory = _make_db()
    step = _make_step("s1", "Plan step", depends_on=[])
    plan = TaskPlan(steps=[step], complexity_reason="requires_approval", triggered=True)

    for task_id in ["task-init-app-1", "task-init-rej-1"]:
        _seed_task_and_timeline(
            factory,
            task_id,
            task_plan=plan,
            constraints={"approval": {"status": "pending"}},
        )
        with session_scope(factory) as session:
            p = ExecutionPlanRepository(session).create(task_id=task_id)
            ExecutionPlanRepository(session).add_node(
                plan_id=p.id, node_id="s1", goal="Plan step", sequence_number=0, depends_on=[]
            )
            state = _make_sample_state(task_id=task_id)
            state.task_plan = plan
            state.approval = ApprovalCheckpoint(required=True, status="pending")
            TemporalTaskStateRepository(session).upsert(
                task_id=task_id, state=_serialize_temporal_task_state(state)
            )

    activities = _make_activities(factory)
    with session_scope(factory) as session:
        t_app = TaskRepository(session).get("task-init-app-1")
        assert t_app is not None
        t_app.constraints = {"approval": {"status": "approved"}}

    loaded_app = activities._get_current_state("task-init-app-1")
    assert loaded_app.approval.status == "approved"
    assert loaded_app.task_plan is not None
    assert loaded_app.decomposed_plan is None

    with session_scope(factory) as session:
        t_rej = TaskRepository(session).get("task-init-rej-1")
        assert t_rej is not None
        t_rej.constraints = {"approval": {"status": "rejected"}}

    asyncio.run(activities.persist_rejected_session_state("task-init-rej-1"))
    with session_scope(factory) as session:
        assert TemporalTaskStateRepository(session).get(task_id="task-init-rej-1") is None


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
    assert len(loaded.decomposed_plan.nodes) == 0


def test_read_only_plan_round_trip_and_fanout() -> None:
    """Read-only plan survives with parallel_safe and execution_mode intact."""
    factory = _make_db()
    task_id = "task-readonly-1"
    node1 = _make_sample_node("scan-1", "Inspect FE", mode="read_only", parallel_safe=True)
    node2 = _make_sample_node("scan-2", "Inspect BE", mode="read_only", parallel_safe=True)
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
    assert len(loaded.decomposed_plan.nodes) == 2
    assert loaded.decomposed_plan.nodes[0].parallel_safe is True
    assert loaded.decomposed_plan.nodes[0].execution_mode == "read_only"


def test_crash_gap_preservation_node_outcomes_invariant() -> None:
    """Terminal evidence in Postgres must NOT be marked parent-merged during rehydration."""
    factory = _make_db()
    task_id = "task-crash-gap-1"
    node = _make_sample_node("step-1", "Execute node")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id=node.node_id,
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
        rehydrated = _rehydrate_dag_state(session, task_id, loaded_state, raw_snapshot=snapshot)
        assert len(rehydrated.node_outcomes) == 0

    selection = asyncio.run(activities._select_next_node(task_id, fanout_contract_enabled=False))
    assert selection["action"] == "merge_terminal"
    assert selection["node_id"] == "step-1"


def test_permission_escalation_across_reader_boundaries() -> None:
    """Blocked node in pruned snapshot correctly triggers and resolves permission escalation."""
    factory = _make_db()
    task_id = "task-perm-esc-1"
    node = _make_sample_node("step-1", "Blocked step")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    blocked_outcome = NodeOutcome(
        node_id="step-1",
        status="blocked",
        result=WorkerResult(
            status="failure",
            summary="Denied",
            next_action_hint="request_higher_permission",
            requested_permission="danger-full-access",
        ),
        attempts=1,
    )
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
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
        db_node = ExecutionPlanRepository(session).get_node(plan.id, "step-1")
        assert db_node is not None
        assert db_node.status == ExecutionPlanNodeStatus.PENDING


def test_fail_closed_validation() -> None:
    """Malformed timeline events and execution plan projection mismatches must fail closed."""
    non_dict_event = types.SimpleNamespace(
        event_type=TimelineEventType.TASK_PLANNED, payload="not-a-dict"
    )
    with pytest.raises(RuntimeError, match="payload must be a dictionary"):
        restore_task_plan_from_events([non_dict_event])
    with pytest.raises(RuntimeError, match="payload must be a dictionary"):
        restore_decomposed_plan_from_events([non_dict_event])

    missing_trig = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_PLANNED.value,
        attempt_number=0,
        sequence_number=1,
        payload={"planning": "generated", "steps": []},
    )
    with pytest.raises(RuntimeError, match="'triggered' field is required"):
        restore_task_plan_from_events([missing_trig])

    non_list_steps = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_PLANNED.value,
        attempt_number=0,
        sequence_number=2,
        payload={"planning": "generated", "triggered": True, "steps": "not-a-list"},
    )
    with pytest.raises(RuntimeError, match="'steps' must be a list"):
        restore_task_plan_from_events([non_list_steps])

    malformed_decomp = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_PLANNED.value,
        attempt_number=0,
        sequence_number=3,
        payload={"decomposition": "not-a-dict"},
    )
    with pytest.raises(RuntimeError, match="Malformed TASK_PLANNED decomposition payload"):
        restore_decomposed_plan_from_events([malformed_decomp])

    missing_keys = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_PLANNED.value,
        attempt_number=0,
        sequence_number=4,
        payload={"other": "data"},
    )
    with pytest.raises(RuntimeError, match="missing planning or decomposition data"):
        restore_task_plan_from_events([missing_keys])

    node = _make_sample_node("step-1", "Node 1")
    plan_model = DecomposedTaskPlan(status="decomposed", nodes=[node])
    sql_plan_mock = MagicMock()
    mock_node = lambda nid: MagicMock(  # noqa: E731
        node_id=nid,
        goal="g",
        sequence_number=0,
        depends_on=[],
        task_spec={},
        node_kind="implement",
        aggregation_role="mutation",
        execution_mode="mutable",
        parallel_safe=False,
    )
    sql_plan_mock.nodes = [mock_node("s1"), mock_node("s2")]
    with pytest.raises(RuntimeError, match="ExecutionPlan node count"):
        validate_decomposed_plan_projection(sql_plan_mock, plan_model)


def test_legacy_rolling_snapshot_compatibility() -> None:
    """Legacy snapshots containing serialized plans load cleanly as fallback."""
    factory = _make_db()
    task_id = "task-legacy-1"
    step = _make_step("s1", "Legacy")
    task_plan = TaskPlan(steps=[step], complexity_reason="legacy", triggered=True)
    node = _make_sample_node("s1", "Legacy")
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node])
    _seed_task_and_timeline(factory, task_id)

    with session_scope(factory) as session:
        _seed_sql_plan_nodes(session, task_id, [node])
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
    assert len(loaded.decomposed_plan.nodes) == 1


def _setup_v2_wave_test_data(
    factory: sessionmaker[Session],
    task_id: str,
    node: DecomposedTaskNode,
) -> tuple[dict[str, Any], list[dict[str, Any] | None]]:
    worker_res = WorkerResult(status="success", summary="Node 1 inspected")
    outcome = NodeOutcome(
        node_id=node.node_id,
        status="completed",
        result=worker_res,
        logical_activity_key="",
    )
    with session_scope(factory) as session:
        plan = _seed_sql_plan_nodes(session, task_id, [node])
        plan_id = str(plan.id)
        key = logical_activity_key(plan_id, node.node_id, 1)
        outcome.logical_activity_key = key
        term_payload = {
            "worker_result": worker_res.model_dump(mode="json"),
            "node_outcome": outcome.model_dump(mode="json"),
        }
        digest = _result_digest(term_payload)
        ExecutionPlanRepository(session).update_node(
            plan_id=plan.id,
            node_id=node.node_id,
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key=key,
            terminal_result_digest=digest,
            terminal_result_payload=term_payload,
        )
        state = _make_sample_state(task_id=task_id)
        state.decomposed_plan = DecomposedTaskPlan(
            triggered=True, status="decomposed", nodes=[node]
        )
        state.node_outcomes = []
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=_serialize_temporal_task_state(state)
        )

    act_req = NodeActivityRequest(
        task_id=task_id,
        plan_id=plan_id,
        node_id=node.node_id,
        logical_attempt=1,
        logical_activity_key=key,
        effective_input_digest="a" * 64,
    )
    selection_data = NodeWaveSelectionV2(
        action="execute_wave",
        wave_id="wave-1",
        items=[
            NodeWaveItem(
                node_id=node.node_id,
                execution_task_queue="task-queue",
                activity_request=act_req,
            )
        ],
    ).model_dump(mode="json")
    result_refs = [
        NodeActivityResultRef(
            node_id=node.node_id,
            logical_activity_key=key,
            status="completed",
            result_digest=digest,
            continuation="continue",
        ).model_dump(mode="json")
    ]
    return selection_data, result_refs


def test_merge_v2_wave_from_pruned_snapshot() -> None:
    """_merge_v2_wave successfully rehydrates decomposed_plan from timeline with pruned snapshot."""
    factory = _make_db()
    task_id = "task-v2-pruned-1"
    node1 = _make_sample_node("node-1", "Node 1", mode="read_only", parallel_safe=True)
    decomposed_plan = DecomposedTaskPlan(triggered=True, status="decomposed", nodes=[node1])
    _seed_task_and_timeline(factory, task_id, decomposed_plan=decomposed_plan)

    selection_data, result_refs = _setup_v2_wave_test_data(factory, task_id, node1)

    activities = _make_activities(factory)
    merge_result = activities._merge_v2_wave(
        task_id=task_id,
        selection_data=selection_data,
        result_refs=result_refs,
    )
    assert merge_result["continuation"] == "continue"

    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is not None
        assert "decomposed_plan" not in snapshot.state
        assert "task_plan" not in snapshot.state
        assert "node_outcomes" not in snapshot.state

    reloaded_state = activities._get_current_state(task_id)
    assert len(reloaded_state.node_outcomes) == 1


def test_no_snapshot_timeline_plan_authority() -> None:
    """_get_current_state with no snapshot authoritatively restores timeline models."""
    factory = _make_db()
    task_id = "task-no-snapshot-1"

    step1 = _make_step("step-1", "Authoritative step 1", depends_on=None, mode="read_only")
    step2 = _make_step("step-2", "Authoritative step 2", depends_on=[])
    task_plan = TaskPlan(
        steps=[step1, step2],
        complexity_reason="authoritative_timeline_reason",
        triggered=True,
    )
    decomp = DecomposedTaskPlan(
        triggered=True,
        status="decomposed",
        reason="authoritative_decomp_reason",
        nodes=[
            _make_sample_node("step-1", "Authoritative step 1", mode="read_only"),
            _make_sample_node("step-2", "Authoritative step 2"),
        ],
    )
    _seed_task_and_timeline(factory, task_id, task_plan=task_plan, decomposed_plan=decomp)

    with session_scope(factory) as session:
        _seed_sql_plan_nodes(session, task_id, decomp.nodes)
        assert TemporalTaskStateRepository(session).get(task_id=task_id) is None

    activities = _make_activities(factory)
    loaded = activities._get_current_state(task_id)

    assert loaded.task_plan is not None
    assert loaded.task_plan.complexity_reason == "authoritative_timeline_reason"
    assert loaded.task_plan.steps[0].depends_on is None
    assert loaded.task_plan.steps[1].depends_on == []
    assert loaded.decomposed_plan is not None
    assert loaded.decomposed_plan.reason == "authoritative_decomp_reason"
    assert loaded.decomposed_plan.nodes[0].title == "Authoritative step 1"
