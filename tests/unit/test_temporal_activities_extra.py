"""Additional tests for orchestrator/temporal/activities.py non-activity helpers."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db.enums import TaskStatus, TimelineEventType
from orchestrator.state import NodeOutcome, OrchestratorState
from orchestrator.temporal.activities import (
    TaskExecutionActivities,
    _approve_permission_escalation,
    _finalize_worker_activity_state,
    _reject_permission_escalation,
    _resolve_permission_escalation_state,
    _worker_state_for_execution,
)
from workers import WorkerResult

# ---------------------------------------------------------------------------
# TaskExecutionActivities._merge_updates
# ---------------------------------------------------------------------------


def _make_activities() -> TaskExecutionActivities:
    svc = MagicMock()
    svc.worker = MagicMock()
    svc.worker_profiles = {}
    svc.enable_worker_profiles = False
    svc.enable_independent_verifier = False
    svc.session_factory = MagicMock()
    svc.workspace_manager = None
    svc.orchestrator_brain = MagicMock()
    svc.progress_notifier = None
    return TaskExecutionActivities(svc)


def test_merge_updates_list_fields_appended():
    activities = _make_activities()
    state_dict = {
        "timeline_events": [{"a": 1}],
        "progress_updates": ["msg1"],
        "friction_reports": [],
        "memory_to_persist": [],
        "errors": [],
        "scout_phase_results": [],
    }
    updates = {
        "timeline_events": [{"b": 2}],
        "progress_updates": ["msg2"],
        "other_key": "val",
    }
    activities._merge_updates(state_dict, updates)
    assert state_dict["timeline_events"] == [{"a": 1}, {"b": 2}]
    assert state_dict["progress_updates"] == ["msg1", "msg2"]
    assert state_dict["other_key"] == "val"


def test_merge_updates_none_values_skipped():
    activities = _make_activities()
    state_dict = {"key": "original"}
    updates = {"key": None}
    activities._merge_updates(state_dict, updates)
    assert state_dict["key"] == "original"


def test_merge_updates_non_dict_updates():
    activities = _make_activities()
    state_dict = {"key": "value"}
    activities._merge_updates(state_dict, None)
    assert state_dict == {"key": "value"}

    activities._merge_updates(state_dict, "not a dict")
    assert state_dict == {"key": "value"}


def test_merge_updates_missing_list_key():
    activities = _make_activities()
    state_dict = {}  # no "timeline_events" key
    updates = {"timeline_events": [{"event": 1}]}
    activities._merge_updates(state_dict, updates)
    assert state_dict["timeline_events"] == [{"event": 1}]


# ---------------------------------------------------------------------------
# TaskExecutionActivities._run_node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_node_none():
    activities = _make_activities()
    result = await activities._run_node(None, {"state": "dict"})
    assert result == {}


@pytest.mark.asyncio
async def test_run_node_with_ainvoke():
    activities = _make_activities()
    node = MagicMock()
    node.ainvoke = AsyncMock(return_value={"result": "value"})
    result = await activities._run_node(node, {"state": "dict"})
    assert result == {"result": "value"}


@pytest.mark.asyncio
async def test_run_node_with_coroutine_function():
    activities = _make_activities()

    async def async_node(state_dict):
        return {"async": "result"}

    result = await activities._run_node(async_node, {"state": "dict"})
    assert result == {"async": "result"}


@pytest.mark.asyncio
async def test_run_node_with_sync_invoke():
    activities = _make_activities()
    node = MagicMock(spec=[])  # no ainvoke, not async
    node.invoke = MagicMock(return_value={"sync": "result"})

    svc = activities.service
    svc._run_blocking = AsyncMock(return_value={"sync": "result"})

    result = await activities._run_node(node, {"state": "dict"})
    assert result == {"sync": "result"}


@pytest.mark.asyncio
async def test_run_node_with_sync_callable():
    activities = _make_activities()

    def sync_node(state_dict):
        return {"sync_call": "result"}

    svc = activities.service
    svc._run_blocking = AsyncMock(return_value={"sync_call": "result"})

    result = await activities._run_node(sync_node, {"state": "dict"})
    assert result == {"sync_call": "result"}


# ---------------------------------------------------------------------------
# TaskExecutionActivities._has_event
# ---------------------------------------------------------------------------


def test_has_event_found():
    activities = _make_activities()
    from orchestrator.state import TaskTimelineEventState

    event = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_INGESTED.value,
        message="created",
        sequence_number=0,
        attempt_number=0,
    )
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    state.timeline_events = [event]

    assert activities._has_event(state, TimelineEventType.TASK_INGESTED) is True


def test_has_event_not_found():
    activities = _make_activities()
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    state.timeline_events = []
    assert activities._has_event(state, TimelineEventType.TASK_INGESTED) is False


def test_has_event_string_event_type():
    """Test that string event types (non-enum) are handled."""
    activities = _make_activities()
    event = MagicMock()
    event.event_type = "task_ingested"  # string, not enum

    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    state.timeline_events = [event]

    assert activities._has_event(state, TimelineEventType.TASK_INGESTED) is True


def test_has_event_task_wide_matches_across_attempts():
    """Test that _has_event matches task-wide across different attempts (HITL resume)."""
    activities = _make_activities()
    from orchestrator.state import TaskTimelineEventState

    event_a0 = TaskTimelineEventState(
        event_type=TimelineEventType.WORKSPACE_PROVISIONED.value,
        message="provisioned",
        sequence_number=0,
        attempt_number=0,
    )
    # Current attempt has advanced to 1 due to lease / resume
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        attempt_count=1,
    )
    state.timeline_events = [event_a0]

    assert activities._has_event(state, TimelineEventType.WORKSPACE_PROVISIONED) is True


def test_serialize_temporal_task_state_excludes_timeline_and_progress():
    """Verify serialization excludes timeline_events/progress_updates but retains cursor."""
    from orchestrator.state import TaskTimelineEventState
    from orchestrator.temporal.activities import _serialize_temporal_task_state

    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        progress_updates=["step 1", "step 2"],
        timeline_events=[
            TaskTimelineEventState(
                event_type=TimelineEventType.TASK_INGESTED.value,
                attempt_number=0,
                sequence_number=0,
            )
        ],
        timeline_persisted_count=42,
    )
    serialized = _serialize_temporal_task_state(state)
    assert "timeline_events" not in serialized
    assert "progress_updates" not in serialized
    assert serialized["timeline_persisted_count"] == 42


def test_get_current_state_rehydrates_timeline_from_db_and_repairs_cursor():
    """Verify _get_current_state replaces legacy snapshot timeline and repairs cursor."""
    from datetime import datetime

    from orchestrator.state import TaskTimelineEventState

    activities = _make_activities()
    created_ts = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

    # Legacy snapshot has in-blob timeline events and stale/larger persisted count
    snapshot_state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        attempt_count=1,
        timeline_events=[
            TaskTimelineEventState(
                event_type="legacy_event",
                attempt_number=0,
                sequence_number=0,
            )
        ],
        timeline_persisted_count=99,
    )
    snapshot = MagicMock()
    snapshot.state = snapshot_state.model_dump(mode="json")

    # Authoritative DB records
    db_row1 = MagicMock()
    db_row1.event_type = TimelineEventType.TASK_INGESTED
    db_row1.attempt_number = 0
    db_row1.sequence_number = 0
    db_row1.message = "ingested"
    db_row1.payload = None
    db_row1.created_at = created_ts

    db_row2 = MagicMock()
    db_row2.event_type = TimelineEventType.WORKSPACE_PROVISIONED
    db_row2.attempt_number = 1
    db_row2.sequence_number = 0
    db_row2.message = "provisioned"
    db_row2.payload = {"w": "id"}
    db_row2.created_at = created_ts

    with (
        patch("orchestrator.temporal.activities.session_scope") as mock_scope,
        patch(
            "orchestrator.temporal.activities.TemporalTaskStateRepository"
        ) as mock_state_repo_cls,
        patch("orchestrator.temporal.activities.TaskRepository") as mock_task_repo_cls,
        patch("orchestrator.temporal.activities.TaskTimelineRepository") as mock_timeline_repo_cls,
    ):
        mock_scope.return_value.__enter__.return_value = MagicMock()
        mock_state_repo_cls.return_value.get.return_value = snapshot
        mock_task_repo_cls.return_value.get.return_value = None
        mock_timeline_repo_cls.return_value.list_by_task.return_value = [db_row1, db_row2]
        mock_timeline_repo_cls.return_value.count_by_attempt.return_value = 1

        rehydrated = activities._get_current_state("task-123")

        # DB records replace legacy snapshot timeline
        assert len(rehydrated.timeline_events) == 2
        assert rehydrated.timeline_events[0].event_type == TimelineEventType.TASK_INGESTED.value
        assert rehydrated.timeline_events[0].message == "ingested"
        prov_event = rehydrated.timeline_events[1]
        assert prov_event.event_type == TimelineEventType.WORKSPACE_PROVISIONED.value
        assert prov_event.payload == {"w": "id"}
        # Stale snapshot count (99) is repaired by authoritative DB count (1)
        assert rehydrated.timeline_persisted_count == 1


@pytest.mark.asyncio
async def test_deliver_result_duplicate_cleans_up_snapshot():
    """Verify that deliver_result deletes temporal snapshot when skipping duplicate completion."""
    from orchestrator.state import TaskTimelineEventState

    activities = _make_activities()
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        timeline_events=[
            TaskTimelineEventState(
                event_type=TimelineEventType.TASK_COMPLETED.value,
                attempt_number=0,
                sequence_number=0,
            )
        ],
    )
    activities._get_current_state = MagicMock(return_value=state)
    activities._delete_temporal_snapshot = MagicMock()
    activities.service._run_blocking = AsyncMock(
        side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)
    )

    await activities.deliver_result("task-done")

    activities._delete_temporal_snapshot.assert_called_once_with("task-done")


# ---------------------------------------------------------------------------
# TaskExecutionActivities._decompose_result
# ---------------------------------------------------------------------------


def test_decompose_result_monolithic():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = TaskExecutionActivities._decompose_result(state)
    assert result.execution_shape == "monolithic"


def test_decompose_result_decomposed():
    from orchestrator.state import DecomposedTaskNode, DecomposedTaskPlan, TaskSpec

    node = DecomposedTaskNode(
        node_id="n1",
        title="Node 1",
        node_kind="implement",
        depends_on=[],
        task_spec=TaskSpec(goal="goal", acceptance_criteria=["ac"], task_type="feature"),
        max_attempts=1,
    )
    plan = DecomposedTaskPlan(status="decomposed", nodes=[node])
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        decomposed_plan=plan,
    )
    result = TaskExecutionActivities._decompose_result(state)
    assert result.execution_shape == "decomposed"


# ---------------------------------------------------------------------------
# _reject_permission_escalation
# ---------------------------------------------------------------------------


def test_reject_permission_escalation_normal():
    session = MagicMock()
    task = MagicMock()
    task.attempt_count = 1
    task.status = TaskStatus.IN_PROGRESS
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})

    with (
        patch("orchestrator.temporal.activities.TaskTimelineRepository") as mock_tl_cls,
        patch("orchestrator.temporal.activities.ExecutionPlanRepository") as mock_plan_cls,
        patch("orchestrator.temporal.activities.TemporalTaskStateRepository") as mock_state_cls,
    ):
        mock_tl_cls.return_value = MagicMock()
        mock_plan_cls.return_value = MagicMock()
        mock_state_cls.return_value = MagicMock()

        _reject_permission_escalation(session, "t1", task, state, blocked=None, plan=None)
        assert task.status == TaskStatus.FAILED


def test_reject_permission_escalation_repair_requested():
    session = MagicMock()
    task = MagicMock()
    task.attempt_count = 1
    task.status = TaskStatus.IN_PROGRESS

    # Simulate repair_requested phase
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    state.completion_loop = state.completion_loop.model_copy(update={"phase": "repair_requested"})

    with (
        patch("orchestrator.temporal.activities.TaskTimelineRepository") as mock_tl_cls,
        patch("orchestrator.temporal.activities.ExecutionPlanRepository") as mock_plan_cls,
        patch("orchestrator.temporal.activities.TemporalTaskStateRepository") as mock_state_cls,
        patch("orchestrator.temporal.activities.apply_repair_rejection"),
    ):
        mock_tl_cls.return_value = MagicMock()
        mock_plan_cls.return_value = MagicMock()
        mock_state_cls.return_value = MagicMock()

        _reject_permission_escalation(session, "t1", task, state, blocked=None, plan=None)
        # Should set back to IN_PROGRESS in repair path
        assert task.status == TaskStatus.IN_PROGRESS


def test_reject_permission_escalation_with_blocked_and_plan():
    session = MagicMock()
    task = MagicMock()
    task.attempt_count = 1
    task.status = TaskStatus.IN_PROGRESS
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})

    blocked = NodeOutcome(
        node_id="n1",
        status="blocked",
        attempts=1,
        result=WorkerResult(status="failure", next_action_hint="request_higher_permission"),
    )
    plan = MagicMock()
    plan.id = "plan-1"

    with (
        patch("orchestrator.temporal.activities.TaskTimelineRepository") as mock_tl_cls,
        patch("orchestrator.temporal.activities.ExecutionPlanRepository") as mock_plan_cls,
        patch("orchestrator.temporal.activities.TemporalTaskStateRepository") as mock_state_cls,
    ):
        mock_tl_cls.return_value = MagicMock()
        plan_repo = MagicMock()
        mock_plan_cls.return_value = plan_repo
        mock_state_cls.return_value = MagicMock()

        _reject_permission_escalation(session, "t1", task, state, blocked=blocked, plan=plan)
        plan_repo.update_node.assert_called_once()


# ---------------------------------------------------------------------------
# _approve_permission_escalation
# ---------------------------------------------------------------------------


def test_approve_permission_escalation_no_blocked():
    session = MagicMock()
    task = MagicMock()
    task.constraints = {}
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="failure", requested_permission="workspace_write"),
    )

    with patch("orchestrator.temporal.activities.TemporalTaskStateRepository") as mock_state_cls:
        mock_state_cls.return_value = MagicMock()
        _approve_permission_escalation(session, "t1", task, state, blocked=None, plan=None)

        assert task.constraints["granted_permission"] == "workspace_write"
        assert task.constraints["permission_escalation_retry"] is True
        assert task.status == TaskStatus.IN_PROGRESS


def test_approve_permission_escalation_with_blocked():
    session = MagicMock()
    task = MagicMock()
    task.constraints = {}
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        node_outcomes=[
            NodeOutcome(
                node_id="n1",
                status="blocked",
                attempts=1,
                result=WorkerResult(status="failure", next_action_hint="request_higher_permission"),
            )
        ],
        result=WorkerResult(status="failure", requested_permission="workspace_write"),
    )

    blocked = state.node_outcomes[0]
    plan = MagicMock()
    plan.id = "plan-1"

    with (
        patch("orchestrator.temporal.activities.TemporalTaskStateRepository") as mock_state_cls,
        patch("orchestrator.temporal.activities.ExecutionPlanRepository") as mock_plan_cls,
        patch("orchestrator.temporal.activities._aggregate_decomposed_results") as mock_agg,
    ):
        mock_state_cls.return_value = MagicMock()
        plan_repo = MagicMock()
        mock_plan_cls.return_value = plan_repo
        mock_agg.return_value = WorkerResult(status="failure")

        _approve_permission_escalation(session, "t1", task, state, blocked=blocked, plan=plan)
        plan_repo.update_node.assert_called_once()


# ---------------------------------------------------------------------------
# _finalize_worker_activity_state (more branches)
# ---------------------------------------------------------------------------


def test_finalize_worker_activity_state_no_repair():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    state_dict = state.model_dump()

    result_state, requires_permission = _finalize_worker_activity_state(
        state_dict, repair_execution=False
    )
    assert requires_permission is False
    # No repair → completion_loop phase unchanged
    assert result_state.completion_loop.phase != "verification_pending"


def test_finalize_worker_activity_state_repair_no_permission():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    state_dict = state.model_dump()

    result_state, requires_permission = _finalize_worker_activity_state(
        state_dict, repair_execution=True
    )
    assert requires_permission is False
    assert result_state.completion_loop.phase == "verification_pending"


def test_finalize_worker_activity_state_requires_permission():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="failure", next_action_hint="request_higher_permission"),
    )
    state_dict = state.model_dump()

    result_state, requires_permission = _finalize_worker_activity_state(
        state_dict, repair_execution=True
    )
    assert requires_permission is True
    # Even with repair_execution=True, if permission needed, don't set verification_pending
    assert result_state.completion_loop.phase != "verification_pending"


def test_finalize_worker_activity_state_strips_escalation_retry():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {"permission_escalation_retry": True, "other": "val"},
        }
    )
    state_dict = state.model_dump()

    result_state, _ = _finalize_worker_activity_state(state_dict, repair_execution=False)
    constraints = result_state.task.constraints
    assert "permission_escalation_retry" not in (constraints or {})


# ---------------------------------------------------------------------------
# _worker_state_for_execution (additional branches)
# ---------------------------------------------------------------------------


def test_worker_state_for_execution_non_repair():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = _worker_state_for_execution(state, repair_execution=False)
    # verification/review not cleared
    assert "verification" in result


def test_worker_state_for_execution_repair_clears_fields():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = _worker_state_for_execution(state, repair_execution=True)
    assert result["verification"] is None
    assert result["review"] is None
    assert result["repair_handoff_requested"] is False


# ---------------------------------------------------------------------------
# _resolve_permission_escalation_state
# ---------------------------------------------------------------------------


def test_resolve_permission_escalation_state_manual_follow_up_not_approved():
    """If phase is manual_follow_up and not approved, return early without changes."""
    session_factory = MagicMock()

    task = MagicMock()
    task.status = TaskStatus.IN_PROGRESS

    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success", next_action_hint="await_manual_follow_up"),
    )
    state.completion_loop = state.completion_loop.model_copy(update={"phase": "manual_follow_up"})

    snapshot = MagicMock()
    snapshot.state = state.model_dump(mode="json")

    with (
        patch("orchestrator.temporal.activities.session_scope") as mock_scope,
        patch("orchestrator.temporal.activities.TaskRepository") as mock_task_repo_cls,
        patch("orchestrator.temporal.activities.TemporalTaskStateRepository") as mock_state_cls,
    ):
        mock_scope.return_value.__enter__.return_value = MagicMock()
        mock_task_repo_cls.return_value.get.return_value = task
        mock_state_cls.return_value.get.return_value = snapshot

        # Should return early without calling approve/reject
        _resolve_permission_escalation_state(session_factory, "t1", approved=False)
        # No state changes expected
        mock_state_cls.return_value.upsert.assert_not_called()
