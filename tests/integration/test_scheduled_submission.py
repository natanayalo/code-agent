"""Transactional scheduled submissions use a single stable session."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from db.base import Base
from db.enums import TaskStatus
from orchestrator.execution import TaskExecutionService
from orchestrator.execution_submission_service import ScheduledSessionBusyError
from orchestrator.execution_types import DeliveryKey
from orchestrator.scheduled_submission import build_scheduled_submission
from repositories import (
    TaskRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from tests.integration.test_repositories_memory import postgres_session_factory  # noqa: F401
from tests.unit.task_execution_service_support import _StaticWorker
from tests.unit.test_schedule_spec import valid_spec


def test_duplicate_precedes_busy_and_terminal_run_allows_next(tmp_path):
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'schedule.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=factory, worker=_StaticWorker())
    submission = build_scheduled_submission(
        schedule_id="weekly", spec=valid_spec(), repo_url="https://example.com/repo"
    )
    first_key = DeliveryKey(channel="schedule", delivery_id="first")
    first = service.create_task_outcome(
        submission, delivery_key=first_key, skip_if_session_busy=True
    )
    duplicate = service.create_task_outcome(
        submission, delivery_key=first_key, skip_if_session_busy=True
    )
    assert duplicate.duplicate and duplicate.task_snapshot.task_id == first.task_snapshot.task_id
    with pytest.raises(ScheduledSessionBusyError):
        service.create_task_outcome(
            submission,
            delivery_key=DeliveryKey(channel="schedule", delivery_id="second"),
            skip_if_session_busy=True,
        )
    with session_scope(factory) as session:
        task = TaskRepository(session).get(first.task_snapshot.task_id)
        assert task is not None
        task.status = TaskStatus.COMPLETED
    with pytest.raises(ScheduledSessionBusyError):
        service.create_task_outcome(
            submission,
            delivery_key=DeliveryKey(channel="schedule", delivery_id="second"),
            skip_if_session_busy=True,
        )
    second = service.create_task_outcome(
        submission,
        delivery_key=DeliveryKey(channel="schedule", delivery_id="third"),
        skip_if_session_busy=True,
    )
    assert second.task_snapshot.task_id != first.task_snapshot.task_id


def test_concurrent_occurrences_only_create_one_task(tmp_path):
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'schedule.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=factory, worker=_StaticWorker())
    submission = build_scheduled_submission(
        schedule_id="weekly", spec=valid_spec(), repo_url="https://example.com/repo"
    )
    barrier = Barrier(2)

    def submit(delivery_id):
        barrier.wait()
        try:
            service.create_task_outcome(
                submission,
                delivery_key=DeliveryKey(channel="schedule", delivery_id=delivery_id),
                skip_if_session_busy=True,
            )
            return "submitted"
        except ScheduledSessionBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["first", "second"]))
    assert sorted(results) == ["busy", "submitted"]
    assert len(service.list_tasks()) == 1


def test_scheduled_task_can_be_cancelled_before_dispatch(tmp_path):
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'schedule.db'}")
    Base.metadata.create_all(engine)
    service = TaskExecutionService(
        session_factory=create_session_factory(engine), worker=_StaticWorker()
    )
    submission = build_scheduled_submission(
        schedule_id="weekly", spec=valid_spec(), repo_url="https://example.com/repo"
    )
    outcome = service.create_task_outcome(
        submission,
        delivery_key=DeliveryKey(channel="schedule", delivery_id="occurrence"),
        skip_if_session_busy=True,
    )
    cancelled = service.cancel_task(task_id=outcome.task_snapshot.task_id)
    assert cancelled is not None and cancelled.status == "cancelled"


@pytest.mark.usefixtures("postgres_session_factory")
def test_postgres_concurrent_occurrences_only_create_one_task(request):
    factory = request.getfixturevalue("postgres_session_factory")
    service = TaskExecutionService(session_factory=factory, worker=_StaticWorker())
    schedule_id = f"test-{uuid4()}"
    submission = build_scheduled_submission(
        schedule_id=schedule_id, spec=valid_spec(), repo_url="https://example.com/repo"
    )
    barrier = Barrier(2)

    def submit(delivery_id):
        barrier.wait()
        try:
            return service.create_task_outcome(
                submission,
                delivery_key=DeliveryKey(
                    channel="schedule", delivery_id=f"{schedule_id}-{delivery_id}"
                ),
                skip_if_session_busy=True,
            ).task_snapshot.session_id
        except ScheduledSessionBusyError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["first", "second"]))
    created_sessions = [result for result in results if result is not None]
    assert len(created_sessions) == 1
    assert len(service.list_tasks(session_id=created_sessions[0])) == 1


@pytest.mark.usefixtures("postgres_session_factory")
def test_postgres_concurrent_duplicate_delivery_creates_one_task(request):
    factory = request.getfixturevalue("postgres_session_factory")
    service = TaskExecutionService(session_factory=factory, worker=_StaticWorker())
    schedule_id = f"test-{uuid4()}"
    submission = build_scheduled_submission(
        schedule_id=schedule_id, spec=valid_spec(), repo_url="https://example.com/repo"
    )
    key = DeliveryKey(channel="schedule", delivery_id=schedule_id)
    barrier = Barrier(2)

    def submit(_index):
        barrier.wait()
        return service.create_task_outcome(submission, delivery_key=key, skip_if_session_busy=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, [1, 2]))
    assert sorted(result.duplicate for result in results) == [False, True]
    assert results[0].task_snapshot.task_id == results[1].task_snapshot.task_id
