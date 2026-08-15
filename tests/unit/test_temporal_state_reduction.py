"""Unit and regression tests for M28.5B Wave 1 Temporal state reduction."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.base import Base, utc_now
from db.enums import ExecutionPlanNodeStatus, TimelineEventType
from orchestrator.graph import _get_previously_failed_workers
from orchestrator.nodes.utils import _progress_update
from orchestrator.state import (
    ApprovalCheckpoint,
    CompletionLoopState,
    DecomposedTaskNode,
    DecomposedTaskPlan,
    NodeOutcome,
    OrchestratorState,
    SessionRef,
    TaskRequest,
    TaskSpec,
    TaskTimelineEventState,
)
from orchestrator.temporal.activities import (
    EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS,
    TaskExecutionActivities,
    _approve_permission_escalation,
    _reject_permission_escalation,
    _serialize_temporal_task_state,
)
from orchestrator.temporal.node_wave import NodeWaveItem, NodeWaveSelectionV2
from repositories import (
    ExecutionPlanRepository,
    SessionStateRepository,
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

    async def _async_run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    svc._run_blocking = _async_run_blocking
    return TaskExecutionActivities(svc)


def _make_sample_state(
    task_id: str = "task-123",
    session_id: str = "session-456",
    progress_updates: list[str] | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        task=TaskRequest(
            task_id=task_id,
            repo_url="https://github.com/example/repo",
            task_text="Run tests",
        ),
        session=SessionRef(
            session_id=session_id,
            user_id="user-1",
            channel="api",
            external_thread_id="thread-1",
        ),
        current_step="generate_task_spec_and_route",
        attempt_count=1,
        progress_updates=progress_updates or ["task ingested", "planning completed"],
    )


# ---------------------------------------------------------------------------
# 1. Serializer Unit Tests
# ---------------------------------------------------------------------------


def test_serialize_temporal_task_state_excludes_progress_updates() -> None:
    """_serialize_temporal_task_state must strip progress_updates from the output dictionary."""
    assert "progress_updates" in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS

    state = _make_sample_state(progress_updates=["msg1", "msg2"])
    serialized = _serialize_temporal_task_state(state)

    assert isinstance(serialized, dict)
    assert "progress_updates" not in serialized
    assert serialized["task"]["task_id"] == "task-123"
    assert serialized["session"]["session_id"] == "session-456"
    assert serialized["attempt_count"] == 1


# ---------------------------------------------------------------------------
# 2. Deserialization & Defaults Tests
# ---------------------------------------------------------------------------


def test_deserialization_defaults_progress_updates_to_empty_list() -> None:
    """Deserializing a snapshot that omits progress_updates defaults it to an empty list."""
    state = _make_sample_state(progress_updates=["msg1", "msg2"])
    serialized = _serialize_temporal_task_state(state)
    assert "progress_updates" not in serialized

    reloaded = OrchestratorState.model_validate(serialized)
    assert reloaded.progress_updates == []
    assert reloaded.task.task_id == "task-123"


# ---------------------------------------------------------------------------
# 3. Rolling Migration / Legacy Compatibility Tests
# ---------------------------------------------------------------------------


def test_rolling_migration_legacy_snapshot_compatibility() -> None:
    """An old snapshot with progress_updates deserializes cleanly; next write strips it."""
    state = _make_sample_state(progress_updates=["legacy msg 1", "legacy msg 2"])
    legacy_serialized = state.model_dump(mode="json")
    assert "progress_updates" in legacy_serialized
    assert legacy_serialized["progress_updates"] == ["legacy msg 1", "legacy msg 2"]

    # 1. Old snapshot reads successfully and preserves in-memory list
    loaded = OrchestratorState.model_validate(legacy_serialized)
    assert loaded.progress_updates == ["legacy msg 1", "legacy msg 2"]

    # 2. Next snapshot write through _serialize_temporal_task_state strips the field
    new_serialized = _serialize_temporal_task_state(loaded)
    assert "progress_updates" not in new_serialized

    # 3. Reading the new snapshot defaults progress_updates to []
    reloaded = OrchestratorState.model_validate(new_serialized)
    assert reloaded.progress_updates == []


# ---------------------------------------------------------------------------
# 4. Behavioral Reload & Legacy Node Helper Tests
# ---------------------------------------------------------------------------


def test_behavioral_reload_legacy_node_accumulation() -> None:
    """Reloading state after snapshot returns empty progress_updates, matching legacy nodes."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-456",
            task_text="Run tests",
        )
        task_id = task.id

    state = _make_sample_state(task_id=task_id, progress_updates=["accumulated update"])
    now = utc_now()
    activities._persist_intermediate_state(
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is not None
        assert "progress_updates" not in snapshot.state

    reloaded_state = activities._get_current_state(task_id)
    assert reloaded_state.progress_updates == []

    # Legacy node accumulation helper remains functional
    accumulated = _progress_update(reloaded_state, "next step message")
    assert accumulated == ["next step message"]


# ---------------------------------------------------------------------------
# 5. Direct Persistence Writes Coverage
# ---------------------------------------------------------------------------


def test_persist_intermediate_state_excludes_progress_updates() -> None:
    """_persist_intermediate_state writes snapshot without progress_updates."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(session_id="sess", task_text="Task text")
        task_id = task.id

    state = _make_sample_state(task_id=task_id, progress_updates=["p1", "p2"])
    now = utc_now()
    activities._persist_intermediate_state(task_id, state, started_at=now, finished_at=now)

    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is not None
        assert "progress_updates" not in snapshot.state


def test_approve_permission_escalation_excludes_progress_updates() -> None:
    """_approve_permission_escalation writes snapshot without progress_updates."""
    factory = _make_db()

    with session_scope(factory) as session:
        task = TaskRepository(session).create(session_id="sess", task_text="Task text")
        task_id = task.id

    state = _make_sample_state(task_id=task_id, progress_updates=["perm_escalation_msg"])
    state.result = WorkerResult(
        status="failure",
        failure_kind="permission_denied",
        requested_permission="sandbox_command:write",
        next_action_hint="request_higher_permission",
    )

    with session_scope(factory) as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        _approve_permission_escalation(
            session=session,
            task_id=task_id,
            task=task,
            state=state,
            blocked=None,
            plan=None,
        )

    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is not None
        assert "progress_updates" not in snapshot.state


def test_reject_permission_escalation_repair_path_excludes_progress_updates() -> None:
    """_reject_permission_escalation on repair path writes snapshot without progress_updates."""
    factory = _make_db()

    with session_scope(factory) as session:
        task = TaskRepository(session).create(session_id="sess", task_text="Task text")
        task_id = task.id

    state = _make_sample_state(task_id=task_id, progress_updates=["repair_msg"])
    state.completion_loop = CompletionLoopState(
        phase="repair_requested",
        repair_source="verifier",
        repair_pass=1,
    )

    with session_scope(factory) as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        _reject_permission_escalation(
            session=session,
            task_id=task_id,
            task=task,
            state=state,
            blocked=None,
            plan=None,
        )

    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is not None
        assert "progress_updates" not in snapshot.state


def _setup_decomposed_node(
    factory: sessionmaker[Session],
) -> tuple[str, str, str]:
    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="sess",
            task_text="Decomposed task",
            constraints={"read_only": True},
        )
        task_id = task.id
        plan = ExecutionPlanRepository(session).create(task_id=task_id)
        plan_id = plan.id
        ExecutionPlanRepository(session).add_node(
            plan_id=plan_id,
            node_id="node-1",
            goal="Inspect repo",
            sequence_number=0,
            task_spec={"goal": "Inspect repo"},
            node_kind="inspect",
            aggregation_role="context",
            execution_mode="read_only",
            parallel_safe=True,
        )
    from orchestrator.node_execution import logical_activity_key

    key = logical_activity_key(plan_id, "node-1", 1)
    return task_id, plan_id, key


def _seed_terminal_node_attempt(
    factory: sessionmaker[Session],
    task_id: str,
    plan_id: str,
    key: str,
    state: OrchestratorState,
) -> None:
    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id, state=state.model_dump(mode="json")
        )
        ExecutionPlanRepository(session).update_node(
            plan_id=plan_id,
            node_id="node-1",
            status=ExecutionPlanNodeStatus.COMPLETED,
            latest_logical_activity_key=key,
            terminal_result_digest="digest-123",
            terminal_result_payload={
                "worker_result": WorkerResult(status="success", summary="done").model_dump(
                    mode="json"
                ),
                "node_outcome": NodeOutcome(
                    node_id="node-1",
                    status="completed",
                    result=WorkerResult(status="success", summary="done"),
                    attempts=1,
                ).model_dump(mode="json"),
            },
            finished_at=utc_now(),
        )


def test_merge_v2_wave_excludes_progress_updates() -> None:
    """_merge_v2_wave direct snapshot persistence writes state without progress_updates."""
    from orchestrator.node_execution import NodeActivityRequest, NodeActivityResultRef

    factory = _make_db()
    activities = _make_activities(factory)
    task_id, plan_id, key = _setup_decomposed_node(factory)

    state = _make_sample_state(task_id=task_id, progress_updates=["fanout_progress"])
    state.decomposed_plan = DecomposedTaskPlan(
        status="decomposed",
        nodes=[
            DecomposedTaskNode(
                node_id="node-1",
                title="Node 1",
                task_spec=TaskSpec(goal="Inspect repo"),
                node_kind="inspect",
                aggregation_role="context",
                execution_mode="read_only",
                parallel_safe=True,
                max_attempts=2,
            )
        ],
    )
    _seed_terminal_node_attempt(factory, task_id, plan_id, key, state)

    act_req = NodeActivityRequest(
        task_id=task_id,
        plan_id=plan_id,
        node_id="node-1",
        logical_attempt=1,
        logical_activity_key=key,
        effective_input_digest="a" * 64,
    )
    selection_data = NodeWaveSelectionV2(
        action="execute_wave",
        wave_id="wave-1",
        items=[
            NodeWaveItem(
                node_id="node-1",
                execution_task_queue="task-queue",
                activity_request=act_req,
            )
        ],
    ).model_dump(mode="json")
    result_refs = [
        NodeActivityResultRef(
            node_id="node-1",
            logical_activity_key=key,
            status="completed",
            result_digest="digest-123",
            continuation="continue",
        ).model_dump(mode="json")
    ]

    activities._merge_v2_wave(
        task_id=task_id,
        selection_data=selection_data,
        result_refs=result_refs,
    )

    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is not None
        assert "progress_updates" not in snapshot.state


# ---------------------------------------------------------------------------
# 6. Terminal Cleanup & Idempotency on Initial Approval Rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_rejected_session_state_deletes_snapshot_and_idempotent() -> None:
    """Initial approval rejection saves session state, deletes snapshot, and retry is harmless."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-reject-1",
            task_text="Build feature X",
            constraints={"approval": {"required": True, "status": "rejected"}},
        )
        task_id = task.id

    state = _make_sample_state(
        task_id=task_id,
        session_id="session-reject-1",
        progress_updates=["awaiting approval"],
    )
    state.approval = ApprovalCheckpoint(required=True, status="rejected")

    # Seed snapshot in DB
    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id,
            state=_serialize_temporal_task_state(state),
        )

    # 1. Execute persist_rejected_session_state
    await activities.persist_rejected_session_state(task_id)

    # 2. Verify session state is durably persisted
    with session_scope(factory) as session:
        session_record = SessionStateRepository(session).get("session-reject-1")
        assert session_record is not None
        assert session_record.active_goal is not None

        # 3. Verify TemporalTaskState snapshot is completely deleted
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is None

    # 4. Activity retry simulation: calling persist_rejected_session_state again succeeds cleanly
    await activities.persist_rejected_session_state(task_id)

    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is None


# ---------------------------------------------------------------------------
# 7. Wave 2: Timeline Events Pruning, Rehydration & Semantic Continuity
# ---------------------------------------------------------------------------


def test_wave_2_serialize_excludes_timeline_events() -> None:
    """_serialize_temporal_task_state must strip timeline_events and retain persisted count."""
    assert "timeline_events" in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS

    state = _make_sample_state()
    state.timeline_events = [
        TaskTimelineEventState(
            event_type=TimelineEventType.TASK_INGESTED.value,
            attempt_number=0,
            sequence_number=0,
        )
    ]
    state.timeline_persisted_count = 10
    serialized = _serialize_temporal_task_state(state)

    assert "timeline_events" not in serialized
    assert serialized["timeline_persisted_count"] == 10


def test_wave_2_deserialization_defaults_timeline_events_to_empty() -> None:
    """Deserializing a pruned snapshot defaults timeline_events to empty list."""
    state = _make_sample_state()
    state.timeline_events = [
        TaskTimelineEventState(
            event_type=TimelineEventType.TASK_INGESTED.value,
            attempt_number=0,
            sequence_number=0,
        )
    ]
    serialized = _serialize_temporal_task_state(state)
    assert "timeline_events" not in serialized

    reloaded = OrchestratorState.model_validate(serialized)
    assert reloaded.timeline_events == []


def test_wave_2_read_through_rehydrates_authoritative_timeline_and_repairs_cursor() -> None:
    """_get_current_state must rehydrate timeline from DB and repair stale snapshot cursors."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-w2-1",
            task_text="Test rehydration",
        )
        task_id = task.id
        timeline_repo = TaskTimelineRepository(session)
        timeline_repo.create(
            task_id=task_id,
            attempt_number=0,
            sequence_number=0,
            event_type=TimelineEventType.TASK_INGESTED,
            message="ingested",
        )
        timeline_repo.create(
            task_id=task_id,
            attempt_number=0,
            sequence_number=1,
            event_type=TimelineEventType.WORKSPACE_PROVISIONED,
            message="provisioned",
        )

    # Create snapshot with legacy in-blob events and stale count
    state = _make_sample_state(task_id=task_id, session_id="session-w2-1")
    state.attempt_count = 0
    state.timeline_events = [
        TaskTimelineEventState(
            event_type="stale_legacy_event",
            attempt_number=0,
            sequence_number=99,
        )
    ]
    state.timeline_persisted_count = 50

    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id,
            state=_serialize_temporal_task_state(state),
        )

    rehydrated = activities._get_current_state(task_id)

    # 1. Authoritative DB events replace any legacy/empty in-blob events
    assert len(rehydrated.timeline_events) == 2
    assert rehydrated.timeline_events[0].event_type == TimelineEventType.TASK_INGESTED.value
    assert rehydrated.timeline_events[1].event_type == TimelineEventType.WORKSPACE_PROVISIONED.value

    # 2. Persisted count cursor is repaired from DB count (2)
    assert rehydrated.timeline_persisted_count == 2


def test_wave_2_semantic_continuity_failed_worker_routing() -> None:
    """_get_previously_failed_workers correctly extracts failed workers from rehydrated state."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-w2-2",
            task_text="Test failed worker routing",
        )
        task_id = task.id
        timeline_repo = TaskTimelineRepository(session)
        timeline_repo.create(
            task_id=task_id,
            attempt_number=0,
            sequence_number=0,
            event_type="worker_dispatched",
            payload={"worker_type": "gemini"},
        )
        timeline_repo.create(
            task_id=task_id,
            attempt_number=0,
            sequence_number=1,
            event_type="worker_failed",
        )

    state = _make_sample_state(task_id=task_id, session_id="session-w2-2")
    state.attempt_count = 1

    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id,
            state=_serialize_temporal_task_state(state),
        )

    rehydrated = activities._get_current_state(task_id)
    failed_workers = _get_previously_failed_workers(rehydrated)

    assert "gemini" in failed_workers
