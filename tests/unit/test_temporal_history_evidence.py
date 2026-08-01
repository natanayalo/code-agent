"""Unit coverage for Temporal history evidence extraction."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio.api.history.v1 import HistoryEvent

from evaluation.temporal_history_evidence import analyze_temporal_history, fetch_temporal_history


def _event(event_id: int, at: datetime) -> HistoryEvent:
    event = HistoryEvent(event_id=event_id)
    event.event_time.FromDatetime(at)
    return event


def _scheduled(event_id: int, at: datetime, activity_type: str) -> HistoryEvent:
    event = _event(event_id, at)
    event.activity_task_scheduled_event_attributes.activity_type.name = activity_type
    return event


def _started(event_id: int, at: datetime, scheduled_id: int, attempt: int = 1) -> HistoryEvent:
    event = _event(event_id, at)
    attrs = event.activity_task_started_event_attributes
    attrs.scheduled_event_id = scheduled_id
    attrs.attempt = attempt
    return event


def _completed(event_id: int, at: datetime, scheduled_id: int) -> HistoryEvent:
    event = _event(event_id, at)
    event.activity_task_completed_event_attributes.scheduled_event_id = scheduled_id
    return event


def test_history_parser_detects_latency_retry_signal_and_fanout_overlap() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    signal = _event(7, start + timedelta(seconds=5))
    signal.workflow_execution_signaled_event_attributes.signal_name = "handle_approval"
    terminal = _event(11, start + timedelta(seconds=9))
    terminal.workflow_execution_completed_event_attributes.SetInParent()
    events = [
        _scheduled(1, start, "run_decomposed_node"),
        _started(2, start + timedelta(seconds=1), 1),
        _scheduled(3, start + timedelta(seconds=2), "run_decomposed_node"),
        _started(4, start + timedelta(seconds=3), 3),
        _completed(5, start + timedelta(seconds=4), 1),
        _completed(6, start + timedelta(seconds=5), 3),
        signal,
        _scheduled(8, start + timedelta(seconds=6), "run_worker"),
        _started(9, start + timedelta(seconds=7), 8, attempt=2),
        _completed(10, start + timedelta(seconds=8), 8),
        terminal,
    ]

    evidence = analyze_temporal_history(
        workflow_id="task-123",
        events=events,
        raw_history={"events": [event.event_id for event in events]},
        raw_history_file="case.json",
    )

    assert evidence.workflow_status == "completed"
    assert evidence.activity_counts == {"run_decomposed_node": 2, "run_worker": 1}
    assert evidence.retry_activity_types == ["run_worker"]
    assert evidence.signal_names == ["handle_approval"]
    assert evidence.fanout_overlap is True
    assert evidence.activities[0].latency_seconds == 4
    assert evidence.history_sha256 == (
        "3c329f66415d05373e3d60e1ca3de8fdd1e7ea259f4f28895c596ba647985f9d"
    )


def test_history_parser_records_failed_and_unfinished_activities() -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    failed = _event(3, start + timedelta(seconds=2))
    failed.activity_task_failed_event_attributes.scheduled_event_id = 1
    evidence = analyze_temporal_history(
        workflow_id="task-1",
        events=[
            _scheduled(1, start, "run_worker"),
            _started(2, start + timedelta(seconds=1), 1),
            failed,
            _scheduled(4, start + timedelta(seconds=3), "deliver_result"),
        ],
        raw_history={},
        raw_history_file="raw.json",
        workflow_status="WORKFLOW_EXECUTION_STATUS_FAILED",
    )

    assert evidence.workflow_status == "failed"
    assert evidence.retry_activity_types == []
    assert [activity.status for activity in evidence.activities] == ["failed", "started"]


def test_history_parser_handles_missing_timestamps_and_running_history() -> None:
    scheduled = HistoryEvent(event_id=1)
    scheduled.activity_task_scheduled_event_attributes.activity_type.name = "run_worker"
    completed = HistoryEvent(event_id=2)
    completed.activity_task_completed_event_attributes.scheduled_event_id = 1

    evidence = analyze_temporal_history(
        workflow_id="task-running",
        events=[scheduled, completed],
        raw_history={},
        raw_history_file="raw.json",
    )

    assert evidence.workflow_status == "running"
    assert evidence.activities[0].latency_seconds is None


def test_fetch_history_writes_raw_private_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    events = [
        _scheduled(1, start, "run_worker"),
        _completed(2, start + timedelta(seconds=1), 1),
    ]

    class FakeHistory:
        run_id = "run-123"

        def __init__(self) -> None:
            self.events = events

        def to_json_dict(self) -> dict:
            return {"events": [1, 2]}

    class FakeHandle:
        async def fetch_history(self):
            return FakeHistory()

        async def describe(self):
            return type("Description", (), {"status": "COMPLETED"})()

    class FakeClient:
        def get_workflow_handle(self, workflow_id: str):
            assert workflow_id == "task-task-123"
            return FakeHandle()

    async def fake_connect(address: str, *, namespace: str):
        assert address == "temporal:7233"
        assert namespace == "baseline"
        return FakeClient()

    monkeypatch.setattr(
        "evaluation.temporal_history_evidence.Client.connect",
        fake_connect,
    )
    raw_path = tmp_path / "private" / "history.json"

    evidence = asyncio.run(
        fetch_temporal_history(
            task_id="task-123",
            address="temporal:7233",
            namespace="baseline",
            raw_history_path=raw_path,
        )
    )

    assert evidence.run_id == "run-123"
    assert evidence.workflow_status == "completed"
    assert raw_path.read_text(encoding="utf-8") == '{"events":[1,2]}\n'
