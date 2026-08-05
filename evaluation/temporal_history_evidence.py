"""Temporal history collection and reliability-signal extraction."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client

from evaluation.temporal_reliability_models import (
    TemporalActivityEvidence,
    TemporalHistoryEvidence,
)

ActivityStatus = Literal["completed", "failed", "timed_out", "cancelled", "started"]

_TERMINAL_ACTIVITY_FIELDS: dict[str, ActivityStatus] = {
    "activity_task_completed_event_attributes": "completed",
    "activity_task_failed_event_attributes": "failed",
    "activity_task_timed_out_event_attributes": "timed_out",
    "activity_task_canceled_event_attributes": "cancelled",
}


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically for immutable bundle digests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _event_time(event: HistoryEvent) -> datetime | None:
    if not event.HasField("event_time"):
        return None
    return event.event_time.ToDatetime(tzinfo=UTC)


def _latency_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _workflow_status_from_events(events: list[HistoryEvent]) -> str:
    terminal_fields = {
        "workflow_execution_completed_event_attributes": "completed",
        "workflow_execution_failed_event_attributes": "failed",
        "workflow_execution_canceled_event_attributes": "cancelled",
        "workflow_execution_terminated_event_attributes": "terminated",
        "workflow_execution_timed_out_event_attributes": "timed_out",
        "workflow_execution_continued_as_new_event_attributes": "continued_as_new",
    }
    for event in reversed(events):
        for field, status in terminal_fields.items():
            if event.HasField(field):
                return status
    return "running"


def _normalize_status(status: object | None, events: list[HistoryEvent]) -> str:
    if status is None:
        return _workflow_status_from_events(events)
    value = getattr(status, "name", status)
    normalized = str(value).lower()
    for prefix in ("workflow_execution_status_", "workflowexecutionstatus."):
        normalized = normalized.removeprefix(prefix)
    if normalized == "canceled":
        return "cancelled"
    return normalized


def _append_unfinished_activities(
    activities: list[TemporalActivityEvidence],
    scheduled: dict[int, tuple[str, datetime | None]],
    attempts: dict[int, int],
) -> None:
    terminal_ids = {activity.scheduled_event_id for activity in activities}
    for scheduled_id, (activity_type, _scheduled_at) in scheduled.items():
        if scheduled_id not in terminal_ids:
            activities.append(
                TemporalActivityEvidence(
                    activity_type=activity_type,
                    scheduled_event_id=scheduled_id,
                    attempt=attempts.get(scheduled_id, 1),
                    status="started",
                    latency_seconds=None,
                )
            )


def _has_fanout_overlap(
    lifecycle: list[tuple[datetime, bool, int]],
) -> bool:
    """Compare activity timestamps because Temporal event IDs can lag wall time."""
    started_at: dict[int, datetime] = {}
    ended_at: dict[int, datetime] = {}
    for at, is_start, scheduled_id in lifecycle:
        if is_start:
            started_at[scheduled_id] = min(at, started_at.get(scheduled_id, at))
        else:
            ended_at[scheduled_id] = max(at, ended_at.get(scheduled_id, at))
    scheduled_ids = sorted(started_at)
    for index, first_id in enumerate(scheduled_ids):
        first_start = started_at[first_id]
        first_end = ended_at.get(first_id)
        for second_id in scheduled_ids[index + 1 :]:
            second_start = started_at[second_id]
            second_end = ended_at.get(second_id)
            if (second_end is None or first_start < second_end) and (
                first_end is None or second_start < first_end
            ):
                return True
    return False


def analyze_temporal_history(
    *,
    workflow_id: str,
    events: Iterable[HistoryEvent],
    raw_history: Any,
    raw_history_file: str,
    run_id: str | None = None,
    workflow_status: object | None = None,
) -> TemporalHistoryEvidence:
    """Derive retries, latency, signals, and fan-out overlap from raw history."""
    event_list = list(events)
    scheduled: dict[int, tuple[str, datetime | None]] = {}
    attempts: dict[int, int] = {}
    activities: list[TemporalActivityEvidence] = []
    retry_types: set[str] = set()
    signals: set[str] = set()
    fanout_lifecycle: list[tuple[datetime, bool, int]] = []

    for event in event_list:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attrs = event.activity_task_scheduled_event_attributes
            scheduled[event.event_id] = (attrs.activity_type.name, _event_time(event))
            continue
        if event.HasField("activity_task_started_event_attributes"):
            attrs = event.activity_task_started_event_attributes
            scheduled_id = attrs.scheduled_event_id
            attempt = max(1, attrs.attempt)
            attempts[scheduled_id] = max(attempts.get(scheduled_id, 1), attempt)
            activity_type = scheduled.get(scheduled_id, ("unknown", None))[0]
            if attempt > 1:
                retry_types.add(activity_type)
            if activity_type == "run_decomposed_node":
                started_at = _event_time(event)
                if started_at is not None:
                    fanout_lifecycle.append((started_at, True, scheduled_id))
            continue
        if event.HasField("workflow_execution_signaled_event_attributes"):
            signals.add(event.workflow_execution_signaled_event_attributes.signal_name)
            continue
        for field, status in _TERMINAL_ACTIVITY_FIELDS.items():
            if not event.HasField(field):
                continue
            attrs = getattr(event, field)
            scheduled_id = attrs.scheduled_event_id
            activity_type, scheduled_at = scheduled.get(scheduled_id, ("unknown", None))
            attempt = attempts.get(scheduled_id, 1)
            activities.append(
                TemporalActivityEvidence(
                    activity_type=activity_type,
                    scheduled_event_id=scheduled_id,
                    attempt=attempt,
                    status=status,
                    latency_seconds=_latency_seconds(scheduled_at, _event_time(event)),
                )
            )
            terminal_at = _event_time(event)
            if activity_type == "run_decomposed_node" and terminal_at is not None:
                fanout_lifecycle.append((terminal_at, False, scheduled_id))
            break

    _append_unfinished_activities(activities, scheduled, attempts)

    activity_counts = Counter(activity_type for activity_type, _ in scheduled.values())
    return TemporalHistoryEvidence(
        workflow_id=workflow_id,
        run_id=run_id,
        workflow_status=_normalize_status(workflow_status, event_list),
        event_count=len(event_list),
        history_sha256=hashlib.sha256(canonical_json_bytes(raw_history)).hexdigest(),
        activities=sorted(activities, key=lambda activity: activity.scheduled_event_id),
        activity_counts=dict(sorted(activity_counts.items())),
        retry_activity_types=sorted(retry_types),
        signal_names=sorted(signals),
        fanout_overlap=_has_fanout_overlap(fanout_lifecycle),
        raw_history_file=raw_history_file,
    )


async def fetch_temporal_history(
    *,
    task_id: str,
    address: str,
    namespace: str,
    raw_history_path: Path,
) -> TemporalHistoryEvidence:
    """Fetch and immediately persist one task history before retention expiry."""
    workflow_id = f"task-{task_id}"
    client = await Client.connect(address, namespace=namespace)
    handle = client.get_workflow_handle(workflow_id)
    history = await handle.fetch_history()
    description = await handle.describe()
    raw_history = history.to_json_dict()
    raw_history_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_history_path.open("xb") as stream:
        stream.write(canonical_json_bytes(raw_history) + b"\n")
    raw_history_path.chmod(0o600)
    return analyze_temporal_history(
        workflow_id=workflow_id,
        run_id=history.run_id,
        workflow_status=description.status,
        events=history.events,
        raw_history=raw_history,
        raw_history_file=raw_history_path.name,
    )
