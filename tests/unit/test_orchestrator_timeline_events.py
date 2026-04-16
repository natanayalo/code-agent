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
    from orchestrator.graph import _timeline_event

    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})
    # First event
    events = _timeline_event(state, TimelineEventType.TASK_INGESTED)
    assert len(events) == 1
    assert events[0].sequence_number == 0

    # Second event chained
    events = _timeline_event(state, TimelineEventType.TASK_CLASSIFIED, base_events=events)
    assert len(events) == 2
    assert events[0].sequence_number == 0
    assert events[1].sequence_number == 1
