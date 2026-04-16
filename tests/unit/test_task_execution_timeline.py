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
        )
    )

    # 4. Second persistence
    service._persist_execution_outcome(
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    # 5. Verify no duplicates in DB
    snapshot = service.get_task(task_id)
    assert len(snapshot.timeline) == 2
    assert snapshot.timeline[0].message == "First"
    assert snapshot.timeline[1].message == "Second"
