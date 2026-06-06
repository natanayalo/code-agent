"""Focused regression tests for delivery dedupe and progress helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.pool import StaticPool

from db.base import Base
from orchestrator import execution as execution_module
from repositories import (
    InboundDeliveryRepository,
    TaskRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


class _StaticWorker(Worker):
    """Minimal worker double used only to initialize the service."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary=f"stubbed: {request.task_text}")


def _make_task_service() -> tuple[execution_module.TaskExecutionService, object]:
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    return service, session_factory


def _submission(*, task_text: str = "Run the task") -> execution_module.TaskSubmission:
    return execution_module.TaskSubmission(
        task_text=task_text,
        session=execution_module.SubmissionSession(
            channel="telegram",
            external_user_id="telegram:user:42",
            external_thread_id="telegram:chat:100",
        ),
    )


def test_create_task_outcome_marks_second_matching_delivery_as_duplicate() -> None:
    """Duplicate delivery keys should reuse the original task instead of persisting another one."""
    service, session_factory = _make_task_service()
    delivery_key = execution_module.DeliveryKey(channel="telegram", delivery_id="dedupe-123")

    first = service.create_task_outcome(
        _submission(task_text="Original task"), delivery_key=delivery_key
    )
    second = service.create_task_outcome(
        _submission(task_text="Original task"), delivery_key=delivery_key
    )

    assert first.duplicate is False
    assert first.persisted is not None
    assert second.duplicate is True
    assert second.persisted is None
    assert second.task_snapshot.task_id == first.task_snapshot.task_id

    with session_scope(session_factory) as session:
        tasks = TaskRepository(session).list_by_session(first.task_snapshot.session_id)
        assert len(tasks) == 1


def test_create_task_outcome_recovers_stale_unassigned_delivery_claim() -> None:
    """A stale delivery row without task_id should be claimed by the next successful submission."""
    service, session_factory = _make_task_service()

    with session_scope(session_factory) as session:
        InboundDeliveryRepository(session).create(
            channel="telegram",
            delivery_id="stale-claim",
            task_id=None,
        )

    outcome = service.create_task_outcome(
        _submission(task_text="Recover stale claim"),
        delivery_key=execution_module.DeliveryKey(channel="telegram", delivery_id="stale-claim"),
    )

    assert outcome.duplicate is False
    assert outcome.persisted is not None

    with session_scope(session_factory) as session:
        delivery = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="telegram",
            delivery_id="stale-claim",
        )
        assert delivery is not None
        assert delivery.task_id == outcome.task_snapshot.task_id


def test_task_summary_prefers_latest_run_summary_and_otherwise_returns_none() -> None:
    """Notification summaries should come from the latest run only when one exists."""
    timestamp = datetime.now(UTC)
    base_summary = execution_module.TaskSummarySnapshot(
        task_id="task-1",
        session_id="session-1",
        status="completed",
        task_text="Task summary",
        created_at=timestamp,
        updated_at=timestamp,
    )
    snapshot_without_run = execution_module.TaskSnapshot(**base_summary.model_dump())
    snapshot_with_run = execution_module.TaskSnapshot(
        **base_summary.model_dump(),
        latest_run=execution_module.WorkerRunSnapshot(
            run_id="run-1",
            worker_type="codex",
            status="success",
            started_at=timestamp,
            summary="Latest worker summary",
        ),
    )

    assert execution_module.TaskExecutionService._task_summary(snapshot_without_run) is None
    assert (
        execution_module.TaskExecutionService._task_summary(snapshot_with_run)
        == "Latest worker summary"
    )


def test_emit_progress_returns_quietly_without_notifier() -> None:
    """Progress emission should no-op cleanly when no notifier is configured."""
    import asyncio

    service, _ = _make_task_service()

    asyncio.run(
        service._emit_progress(
            _submission(task_text="No notifier"),
            execution_module._PersistedTaskContext(
                user_id="user-1",
                session_id="session-1",
                channel="telegram",
                external_thread_id="thread-1",
                task_id="task-1",
                attempt_count=1,
            ),
            phase="running",
            summary="still working",
        )
    )


def test_emit_progress_passes_structured_event_to_notifier() -> None:
    """Progress emission should hand a structured event to the configured notifier."""
    import asyncio

    class _RecordingNotifier:
        def __init__(self) -> None:
            self.calls: list[
                tuple[execution_module.TaskSubmission, execution_module.ProgressEvent]
            ] = []

        async def notify(
            self,
            *,
            submission: execution_module.TaskSubmission,
            event: execution_module.ProgressEvent,
        ) -> None:
            self.calls.append((submission, event))

    service, _ = _make_task_service()
    notifier = _RecordingNotifier()
    service.progress_notifier = notifier
    submission = _submission(task_text="Notify operator")

    asyncio.run(
        service._emit_progress(
            submission,
            execution_module._PersistedTaskContext(
                user_id="user-1",
                session_id="session-1",
                channel="telegram",
                external_thread_id="thread-42",
                task_id="task-42",
                attempt_count=2,
            ),
            phase="running",
            summary="worker is executing",
        )
    )

    assert len(notifier.calls) == 1
    recorded_submission, event = notifier.calls[0]
    assert recorded_submission.task_text == "Notify operator"
    assert event.phase == "running"
    assert event.task_id == "task-42"
    assert event.session_id == "session-1"
    assert event.channel == "telegram"
    assert event.external_thread_id == "thread-42"
    assert event.summary == "worker is executing"
