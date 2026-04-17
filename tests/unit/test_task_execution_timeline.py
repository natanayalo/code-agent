"""Unit tests for task execution service timeline persistence (T-090)."""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base, utc_now
from db.enums import TimelineEventType
from orchestrator import (
    OrchestratorState,
    RouteDecision,
    SessionRef,
    TaskRequest,
    WorkerResult,
)
from orchestrator.execution import TaskExecutionService
from repositories import (
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest


class _StaticWorker(Worker):
    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary="done")


@pytest.fixture
def session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def service(session_factory):
    return TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )


def test_persist_execution_outcome_saves_timeline(session_factory, service) -> None:
    """The execution service must persist timeline events from the orchestrator state."""
    task_id = "task-1"
    now = utc_now()

    state = OrchestratorState(
        current_step="persist_memory",
        session=SessionRef(
            session_id="session-1",
            user_id="user-1",
            channel="http",
            external_thread_id="thread-1",
            active_task_id=task_id,
        ),
        task=TaskRequest(task_id=task_id, task_text="hello"),
        timeline_events=[
            {
                "event_type": TimelineEventType.TASK_INGESTED,
                "message": "Ingested",
                "created_at": now,
            },
            {
                "event_type": TimelineEventType.WORKER_SELECTED,
                "message": "Selected",
                "payload": {"w": "c"},
            },
        ],
        route=RouteDecision(chosen_worker="codex", route_reason="test"),
        result=WorkerResult(status="success", summary="ok"),
    )

    # We need a real task in the DB for FK constraints
    with session_scope(session_factory) as session:
        from repositories import SessionRepository, TaskRepository, UserRepository

        user = UserRepository(session).create(external_user_id="user-1")
        conv = SessionRepository(session).create(
            user_id=user.id, channel="http", external_thread_id="thread-1"
        )
        task = TaskRepository(session).create(session_id=conv.id, task_text="hello")
        task_id = task.id

    state = OrchestratorState(
        current_step="persist_memory",
        session=SessionRef(
            session_id="session-1",
            user_id="user-1",
            channel="http",
            external_thread_id="thread-1",
            active_task_id=task_id,
        ),
        task=TaskRequest(task_id=task_id, task_text="hello"),
        timeline_events=[
            {
                "event_type": TimelineEventType.TASK_INGESTED,
                "message": "Ingested",
                "created_at": now,
            },
            {
                "event_type": TimelineEventType.WORKER_SELECTED,
                "message": "Selected",
                "payload": {"w": "c"},
            },
        ],
        route=RouteDecision(chosen_worker="codex", route_reason="test"),
        result=WorkerResult(status="success", summary="ok"),
    )

    service._persist_execution_outcome(
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    # Verify snapshot includes timeline
    snapshot = service.get_task(task_id)
    assert snapshot is not None
    assert len(snapshot.timeline) == 2
    assert snapshot.timeline[0].event_type == TimelineEventType.TASK_INGESTED
    assert snapshot.timeline[0].message == "Ingested"
    assert snapshot.timeline[1].event_type == TimelineEventType.WORKER_SELECTED
    assert snapshot.timeline[1].message == "Selected"
    assert snapshot.timeline[1].payload == {"w": "c"}


def test_persist_execution_outcome_deduplicates_events(session_factory, service) -> None:
    """The execution service must skip events that are already persisted (Resume scenario)."""
    now = utc_now()

    # 1. Setup task and initial state
    with session_scope(session_factory) as session:
        from repositories import SessionRepository, TaskRepository, UserRepository

        user = UserRepository(session).create(external_user_id="user-1")
        conv = SessionRepository(session).create(
            user_id=user.id, channel="http", external_thread_id="thread-resume"
        )
        task = TaskRepository(session).create(session_id=conv.id, task_text="resume me")
        task_id = task.id

    initial_event = {
        "event_type": TimelineEventType.TASK_INGESTED,
        "message": "First",
        "created_at": now,
        "sequence_number": 0,
    }

    state = OrchestratorState(
        current_step="persist_memory",
        session=SessionRef(
            session_id="session-1",
            user_id="user-1",
            channel="http",
            external_thread_id="thread-resume",
            active_task_id=task_id,
        ),
        task=TaskRequest(task_id=task_id, task_text="resume me"),
        timeline_events=[initial_event],
        route=RouteDecision(chosen_worker="codex", route_reason="test"),
        result=WorkerResult(status="success", summary="ok"),
    )

    # 2. First persistence (e.g. at a pause)
    service._persist_execution_outcome(
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    # 3. Simulate resume: state has old events + new ones
    from orchestrator.state import TaskTimelineEventState

    state.timeline_events.append(
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_SELECTED,
            message="Second",
            created_at=now,
            sequence_number=1,
        )
    )

    # 4. Second persistence
    state.timeline_persisted_count = 1  # Simulate DB-based re-initialization on resume
    service._persist_execution_outcome(
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    # 5. Verify no duplicates in DB
    snapshot = service.get_task(task_id)
    assert len(snapshot.timeline) == 2
    assert snapshot.timeline[1].message == "Second"


def test_persist_execution_outcome_deduplicates_retry_attempts(session_factory, service) -> None:
    """The execution service must deduplicate correctly across multiple retry attempts."""
    now = utc_now()
    from orchestrator.state import TaskTimelineEventState

    # 1. Setup task
    with session_scope(session_factory) as session:
        from repositories import SessionRepository, TaskRepository, UserRepository

        user = UserRepository(session).create(external_user_id="user-1")
        conv = SessionRepository(session).create(
            user_id=user.id, channel="http", external_thread_id="thread-retry"
        )
        task = TaskRepository(session).create(session_id=conv.id, task_text="retry me")
        task_id = task.id

    # 2. Simulate Attempt 0 (Started and Interrupted)
    state_0 = OrchestratorState(
        attempt_count=0,
        session=SessionRef(
            session_id="session-1",
            user_id="user-1",
            channel="http",
            external_thread_id="thread-retry",
            active_task_id=task_id,
        ),
        task=TaskRequest(task_id=task_id, task_text="retry me"),
        timeline_events=[
            TaskTimelineEventState(
                event_type=TimelineEventType.TASK_INGESTED,
                message="A0-1",
                attempt_number=0,
                sequence_number=0,
            )
        ],
    )
    service._persist_execution_outcome(
        task_id=task_id, state=state_0, started_at=now, finished_at=now
    )

    # 3. Simulate Attempt 1 (Retry - starts with fresh state.timeline_events)
    state_1 = OrchestratorState(
        attempt_count=1,
        session=SessionRef(
            session_id="session-1",
            user_id="user-1",
            channel="http",
            external_thread_id="thread-retry",
            active_task_id=task_id,
        ),
        task=TaskRequest(task_id=task_id, task_text="retry me"),
        timeline_events=[
            TaskTimelineEventState(
                event_type=TimelineEventType.TASK_INGESTED,
                message="A1-1",
                attempt_number=1,
                sequence_number=0,
            )
        ],
    )
    # This should NOT be skipped because although DB count is 1,
    # A1-1 is the first entry for attempt 1
    service._persist_execution_outcome(
        task_id=task_id, state=state_1, started_at=now, finished_at=now
    )

    # 4. Simulate Attempt 1 Resume (add A1-2)
    state_1.timeline_events.append(
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_SELECTED,
            message="A1-2",
            attempt_number=1,
            sequence_number=1,
        )
    )
    # This should deduplicate A1-1 and only add A1-2
    state_1.timeline_persisted_count = 1  # Previous event for this attempt already in DB
    service._persist_execution_outcome(
        task_id=task_id, state=state_1, started_at=now, finished_at=now
    )

    # 5. Verify DB contents
    snapshot = service.get_task(task_id)
    assert len(snapshot.timeline) == 3
    assert snapshot.timeline[0].message == "A0-1"
    assert snapshot.timeline[0].attempt_number == 0
    assert snapshot.timeline[1].message == "A1-1"
    assert snapshot.timeline[1].attempt_number == 1
    assert snapshot.timeline[2].message == "A1-2"
    assert snapshot.timeline[2].attempt_number == 1
