"""Unit tests to verify orchestrator graph nodes emit timeline events (T-090)."""

from __future__ import annotations

from db.enums import TimelineEventType
from orchestrator.graph import (
    build_choose_worker_node,
    check_approval,
    classify_task,
    dispatch_job,
    ingest_task,
    load_memory,
    plan_task,
    summarize_result,
    verify_result,
)
from orchestrator.state import OrchestratorState


def test_ingest_task_emits_event():
    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})
    res = ingest_task(state)
    assert len(res["timeline_events"]) == 1
    assert res["timeline_events"][0].event_type == TimelineEventType.TASK_INGESTED


def test_classify_task_emits_event():
    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})
    res = classify_task(state)
    assert len(res["timeline_events"]) == 1
    assert res["timeline_events"][0].event_type == TimelineEventType.TASK_CLASSIFIED
    assert res["timeline_events"][0].payload["task_kind"] == "implementation"


def test_load_memory_emits_event():
    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})
    res = load_memory(state)
    assert len(res["timeline_events"]) == 1
    assert res["timeline_events"][0].event_type == TimelineEventType.MEMORY_LOADED


def test_plan_task_emits_event_when_generated():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Refactor architecture across files"},
            "task_kind": "architecture",
        }
    )
    res = plan_task(state)
    assert len(res["timeline_events"]) == 1
    assert res["timeline_events"][0].event_type == TimelineEventType.TASK_CLASSIFIED
    assert res["timeline_events"][0].payload["planning"] == "generated"


def test_choose_worker_emits_event():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "hello"}, "task_kind": "implementation"}
    )
    choose_node = build_choose_worker_node(frozenset({"codex"}))
    res = choose_node(state)
    assert len(res["timeline_events"]) == 1
    assert res["timeline_events"][0].event_type == TimelineEventType.WORKER_SELECTED
    assert res["timeline_events"][0].payload["chosen_worker"] == "codex"


def test_check_approval_emits_event():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "hello"}, "route": {"chosen_worker": "codex"}}
    )
    res = check_approval(state)
    assert len(res["timeline_events"]) == 1
    assert res["timeline_events"][0].event_type == TimelineEventType.APPROVAL_REQUESTED


def test_dispatch_job_emits_event():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "hello"},
            "route": {"chosen_worker": "codex", "route_reason": "test"},
            "attempt_count": 0,
        }
    )
    res = dispatch_job(state)
    assert len(res["timeline_events"]) == 1
    assert res["timeline_events"][0].event_type == TimelineEventType.WORKER_DISPATCHED


def test_verify_result_emits_event():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "hello"}, "result": {"status": "success", "summary": "done"}}
    )
    res = verify_result(state)
    assert len(res["timeline_events"]) == 2
    assert res["timeline_events"][0].event_type == TimelineEventType.VERIFICATION_STARTED
    assert res["timeline_events"][1].event_type == TimelineEventType.VERIFICATION_COMPLETED


def test_summarize_result_emits_event():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "hello"},
            "result": {"status": "success", "summary": "done"},
            "dispatch": {"worker_type": "codex"},
        }
    )
    res = summarize_result(state)
    assert len(res["timeline_events"]) == 1
    assert res["timeline_events"][0].event_type == TimelineEventType.TASK_COMPLETED


def test_timeline_sequence_stability():
    """Events must have monotonic sequence numbers even when emitted in the same step."""
    from orchestrator.graph import _timeline_events

    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})
    # Batch emission
    res = _timeline_events(
        state,
        (TimelineEventType.TASK_INGESTED, None, None),
        (TimelineEventType.TASK_CLASSIFIED, None, None),
        (TimelineEventType.MEMORY_LOADED, None, None),
    )
    events = res["timeline_events"]
    assert len(events) == 3
    assert events[0].event_type == TimelineEventType.TASK_INGESTED
    assert events[0].sequence_number == 0
    assert events[1].event_type == TimelineEventType.TASK_CLASSIFIED
    assert events[1].sequence_number == 1
    assert events[2].event_type == TimelineEventType.MEMORY_LOADED
    assert events[2].sequence_number == 2


def test_timeline_sequence_incorporates_persisted_count():
    """Sequence numbers must incorporate timeline_persisted_count for monotonic resumes."""
    from orchestrator.graph import _timeline_events
    from orchestrator.state import TaskTimelineEventState

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "t1", "task_text": "demo"},
            "attempt_count": 1,
            "timeline_persisted_count": 5,  # 5 events already in DB
            "timeline_events": [],
        }
    )

    result = _timeline_events(
        state,
        (TimelineEventType.WORKER_SELECTED, None, None),
    )

    events = result["timeline_events"]
    assert len(events) == 1
    assert isinstance(events[0], TaskTimelineEventState)
    # Sequence should start at persisted_count (5)
    assert events[0].sequence_number == 5
    assert events[0].attempt_number == 1


def test_timeline_sequence_increments_across_calls():
    """Sequence numbers must increment based on existing events in the list."""
    from orchestrator.graph import _timeline_events
    from orchestrator.state import TaskTimelineEventState

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "hello"},
            "attempt_count": 0,
            "timeline_events": [
                TaskTimelineEventState(
                    event_type=TimelineEventType.TASK_INGESTED,
                    attempt_number=0,
                    sequence_number=i,
                    message=f"event {i}",
                )
                for i in range(5)
            ],
        }
    )
    res = _timeline_events(state, (TimelineEventType.TASK_CLASSIFIED, None, None))
    assert res["timeline_events"][0].sequence_number == 5
