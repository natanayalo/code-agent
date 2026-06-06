"""Focused integration tests for timeline and artifact repository helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import ArtifactType, TimelineEventType
from repositories import (
    ArtifactRepository,
    SessionRepository,
    TaskRepository,
    TaskTimelineRepository,
    UserRepository,
    WorkerRunRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


@pytest.fixture
def session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _create_task_and_run(session) -> tuple[str, str]:
    user = UserRepository(session).create(
        external_user_id="timeline-user",
        display_name="Timeline User",
    )
    conversation_session = SessionRepository(session).create(
        user_id=user.id,
        channel="http",
        external_thread_id=f"thread-{user.id}",
    )
    task = TaskRepository(session).create(
        session_id=conversation_session.id,
        task_text="Persist helper regressions",
    )
    run = WorkerRunRepository(session).create(
        task_id=task.id,
        session_id=conversation_session.id,
        worker_type="codex",
        started_at=datetime.now(UTC),
        status="running",
    )
    return task.id, run.id


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def test_artifact_repository_lists_in_creation_order_and_deletes_all(session_factory) -> None:
    """Artifact helpers should preserve insertion order and delete all run artifacts."""
    with session_scope(session_factory) as session:
        _, run_id = _create_task_and_run(session)
        repo = ArtifactRepository(session)

        first = repo.create(
            run_id=run_id,
            artifact_type=ArtifactType.LOG,
            name="stdout.log",
            uri="artifacts/stdout.log",
        )
        second = repo.create(
            run_id=run_id,
            artifact_type=ArtifactType.RESULT_SUMMARY,
            name="result.md",
            uri="artifacts/result.md",
        )

        assert [artifact.id for artifact in repo.list_by_run(run_id)] == [first.id, second.id]
        assert repo.delete_by_run(run_id) == 2
        assert repo.list_by_run(run_id) == []


def test_task_timeline_repository_preserves_explicit_created_at_and_batches_events(
    session_factory,
) -> None:
    """Timeline helpers should honor explicit timestamps and default missing ones consistently."""
    explicit_time = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    with session_scope(session_factory) as session:
        task_id, _ = _create_task_and_run(session)
        repo = TaskTimelineRepository(session)

        created = repo.create(
            task_id=task_id,
            event_type=TimelineEventType.TASK_INGESTED,
            attempt_number=0,
            sequence_number=0,
            message="Task ingested.",
            created_at=explicit_time,
        )
        repo.create_batch(task_id=task_id, events=[])
        repo.create_batch(
            task_id=task_id,
            events=[
                {
                    "attempt_number": 0,
                    "sequence_number": 1,
                    "event_type": TimelineEventType.TASK_CLASSIFIED,
                    "message": "Task classified.",
                    "created_at": explicit_time,
                },
                {
                    "attempt_number": 1,
                    "sequence_number": 0,
                    "event_type": TimelineEventType.TASK_PLANNED,
                    "message": "Retry planned.",
                },
            ],
        )

        events = repo.list_by_task(task_id)
        assert created.created_at == explicit_time
        assert created.updated_at == explicit_time
        assert [event.event_type for event in events] == [
            TimelineEventType.TASK_INGESTED,
            TimelineEventType.TASK_CLASSIFIED,
            TimelineEventType.TASK_PLANNED,
        ]
        assert _as_naive_utc(events[1].created_at) == _as_naive_utc(explicit_time)
        assert _as_naive_utc(events[1].updated_at) == _as_naive_utc(explicit_time)
        assert events[2].created_at is not None
        assert events[2].updated_at == events[2].created_at
        assert repo.count_by_attempt(task_id, 0) == 2
        assert repo.count_by_attempt(task_id, 1) == 1
