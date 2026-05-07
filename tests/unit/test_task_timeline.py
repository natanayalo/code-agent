"""Unit tests for the task timeline (T-090)."""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base, utc_now
from db.enums import TimelineEventType
from db.models import TaskTimelineEvent
from repositories import (
    TaskRepository,
    TaskTimelineRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


@pytest.fixture
def session_factory():
    """Create a clean in-memory database for each test."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    return factory


def test_task_timeline_repository_creates_and_lists_events(session_factory) -> None:
    """Timeline events can be persisted and retrieved in chronological order."""
    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        timeline_repo = TaskTimelineRepository(session)

        # 1. Setup a task
        task = task_repo.create(
            session_id="session-123",
            task_text="Build a timeline",
        )
        task_id = task.id

        # 2. Create events
        t1 = utc_now()
        timeline_repo.create(
            task_id=task_id,
            event_type=TimelineEventType.TASK_INGESTED,
            message="Task received.",
            created_at=t1,
            sequence_number=0,
        )

        timeline_repo.create(
            task_id=task_id,
            event_type=TimelineEventType.WORKER_SELECTED,
            message="Worker chosen.",
            payload={"worker": "codex"},
            sequence_number=1,
        )

        # 10. Verify listing
        events = timeline_repo.list_by_task(task_id)
        assert len(events) == 2
        assert events[0].event_type == TimelineEventType.TASK_INGESTED
        assert events[0].message == "Task received."
        assert events[1].event_type == TimelineEventType.WORKER_SELECTED
        assert events[1].payload == {"worker": "codex"}


def test_task_relationship_includes_timeline_events(session_factory) -> None:
    """The Task model relationship provides easy access to its timeline."""
    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        timeline_repo = TaskTimelineRepository(session)

        task = task_repo.create(
            session_id="session-456",
            task_text="Relational check",
        )

        timeline_repo.create(
            task_id=task.id,
            event_type=TimelineEventType.TASK_INGESTED,
            message="Step 1",
        )

        # Refresh from DB
        session.expire_all()
        persisted_task = task_repo.get(task.id)
        assert persisted_task is not None
        assert len(persisted_task.timeline_events) == 1
        assert persisted_task.timeline_events[0].message == "Step 1"


def test_create_next_for_attempt_assigns_monotonic_sequence_numbers(session_factory) -> None:
    """Auto-sequenced timeline inserts should use the next available sequence number."""
    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        timeline_repo = TaskTimelineRepository(session)

        task = task_repo.create(
            session_id="session-789",
            task_text="Sequence check",
        )

        first = timeline_repo.create_next_for_attempt(
            task_id=task.id,
            attempt_number=task.attempt_count,
            event_type=TimelineEventType.TASK_INGESTED,
            message="First",
        )
        second = timeline_repo.create_next_for_attempt(
            task_id=task.id,
            attempt_number=task.attempt_count,
            event_type=TimelineEventType.WORKER_SELECTED,
            message="Second",
        )

        assert first.sequence_number == 0
        assert second.sequence_number == 1


def test_timeline_event_type_validation() -> None:
    """Invalid event types should be rejected or coerced if possible."""
    # This is partially handled by the @validates but also by the Enum column
    event = TaskTimelineEvent(
        task_id="task-1",
        event_type="task_ingested",  # Should be coerced to Enum
    )
    assert event.event_type == TimelineEventType.TASK_INGESTED
