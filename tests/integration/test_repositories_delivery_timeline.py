"""Integration tests for delivery and timeline repositories."""

from __future__ import annotations

from datetime import UTC, datetime

from db.enums import TimelineEventType
from repositories import (
    InboundDeliveryRepository,
    TaskRepository,
    TaskTimelineRepository,
    session_scope,
)


def test_inbound_delivery_repository_attaches_tasks_once(session_factory) -> None:
    """Inbound delivery dedupe claims should attach a task only to unassigned rows."""
    with session_scope(session_factory) as session:
        delivery_repo = InboundDeliveryRepository(session)

        created = delivery_repo.create(channel="telegram", delivery_id="delivery-1")
        assert created.task_id is None
        fetched = delivery_repo.get_by_channel_delivery(
            channel="telegram",
            delivery_id="delivery-1",
        )
        assert fetched is not None
        assert fetched.id == created.id

        attached = delivery_repo.attach_task_if_unassigned(
            channel="telegram",
            delivery_id="delivery-1",
            task_id="task-1",
        )
        assert attached is not None
        assert attached.task_id == "task-1"
        assert (
            delivery_repo.attach_task_if_unassigned(
                channel="telegram",
                delivery_id="delivery-1",
                task_id="task-2",
            )
            is None
        )
        assert (
            delivery_repo.get_by_channel_delivery(channel="telegram", delivery_id="missing") is None
        )


def test_task_timeline_repository_supports_batch_creation(session_factory) -> None:
    """Timeline batch creation should preserve provided timestamps and ignore empty batches."""
    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        timeline_repo = TaskTimelineRepository(session)
        task = task_repo.create(session_id="session-timeline", task_text="timeline batch")

        timeline_repo.create_batch(task_id=task.id, events=[])
        created_at = datetime(2026, 1, 1, tzinfo=UTC)
        timeline_repo.create_batch(
            task_id=task.id,
            events=[
                {
                    "attempt_number": 0,
                    "sequence_number": 0,
                    "event_type": TimelineEventType.TASK_INGESTED,
                    "message": "ingested",
                    "created_at": created_at,
                },
                {
                    "attempt_number": 0,
                    "sequence_number": 1,
                    "event_type": TimelineEventType.WORKER_SELECTED,
                    "message": "worker selected",
                },
            ],
        )

        events = timeline_repo.list_by_task(task.id)
        assert len(events) == 2
        assert events[0].created_at == created_at.replace(tzinfo=None)
        assert events[0].updated_at == created_at.replace(tzinfo=None)
        assert events[1].message == "worker selected"
