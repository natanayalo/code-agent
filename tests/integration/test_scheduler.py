"""Integration tests for the ScoutScheduler proactively spawning tasks."""

from datetime import UTC, datetime

from apps.api.config import SystemConfig
from apps.api.scheduler import ScoutScheduler
from db.enums import TaskStatus
from db.models import Task
from orchestrator.execution import TaskExecutionService
from orchestrator.execution_types import TaskSubmission
from tests.integration.task_endpoints_support import _default_worker


def test_scheduler_respects_disabled_flag(session_factory) -> None:
    worker = _default_worker()
    task_service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
    )
    config = SystemConfig(
        default_image="test",
        workspace_root="/tmp",
        scout_scheduler_enabled=False,
    )
    scheduler = ScoutScheduler(task_service, config)

    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    scheduler.tick(now)

    tasks = task_service.list_tasks()
    assert len(tasks) == 0


def test_scheduler_triggers_idle_when_system_is_idle(session_factory) -> None:
    worker = _default_worker()
    task_service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
    )
    config = SystemConfig(
        default_image="test",
        workspace_root="/tmp",
        scout_scheduler_enabled=True,
        scout_repo_url="https://github.com/foo/bar",
        scout_idle_trigger_minutes=30,
        scout_schedule_interval_minutes=0,
    )
    scheduler = ScoutScheduler(task_service, config)

    # Simulate an idle system by jumping 31 minutes into the future
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    scheduler._last_busy_time = base_time

    trigger_time = datetime(2025, 1, 1, 12, 31, 0, tzinfo=UTC)
    scheduler.tick(trigger_time)

    tasks = task_service.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].repo_url == "https://github.com/foo/bar"
    assert "scout" in tasks[0].task_text.lower()

    # Mark the first task as COMPLETED so it is no longer busy
    with session_factory() as session:
        task = session.get(Task, tasks[0].task_id)
        task.status = TaskStatus.COMPLETED
        session.commit()

    # Tick again at the same hour, should be deduped by DeliveryKey
    scheduler.tick(datetime(2025, 1, 1, 12, 32, 0, tzinfo=UTC))
    tasks_after = task_service.list_tasks()
    assert len(tasks_after) == 1


def test_scheduler_does_not_trigger_when_execution_busy(session_factory) -> None:
    worker = _default_worker()
    task_service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
    )
    config = SystemConfig(
        default_image="test",
        workspace_root="/tmp",
        scout_scheduler_enabled=True,
        scout_repo_url="https://github.com/foo/bar",
        scout_idle_trigger_minutes=30,
        scout_schedule_interval_minutes=0,
    )

    # Make task service busy by creating a task (will be in PENDING state)
    task_service.create_task_outcome(TaskSubmission(task_text="test"))

    scheduler = ScoutScheduler(task_service, config)
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    scheduler._last_busy_time = base_time

    # Wait 31 minutes, but system is busy!
    trigger_time = datetime(2025, 1, 1, 12, 31, 0, tzinfo=UTC)
    scheduler.tick(trigger_time)

    tasks = task_service.list_tasks()
    assert len(tasks) == 1
    assert scheduler._last_busy_time == trigger_time


def test_scheduler_triggers_schedule_trigger(session_factory) -> None:
    worker = _default_worker()
    task_service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
    )
    config = SystemConfig(
        default_image="test",
        workspace_root="/tmp",
        scout_scheduler_enabled=True,
        scout_repo_url="https://github.com/foo/bar",
        scout_idle_trigger_minutes=0,
        scout_schedule_interval_minutes=60,
    )
    scheduler = ScoutScheduler(task_service, config)

    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    scheduler._last_busy_time = base_time

    trigger_time = datetime(2025, 1, 1, 12, 31, 0, tzinfo=UTC)
    scheduler.tick(trigger_time)

    tasks = task_service.list_tasks()
    assert len(tasks) == 1

    # Tick again in the same 60 min period, should be deduped (is_busy will be True anyway)
    scheduler.tick(datetime(2025, 1, 1, 12, 45, 0, tzinfo=UTC))
    assert len(task_service.list_tasks()) == 1

    # Mark the first task as COMPLETED so it is no longer busy
    with session_factory() as session:
        task = session.get(Task, tasks[0].task_id)
        task.status = TaskStatus.COMPLETED
        session.commit()

    # Tick in the next 60 min period, should trigger
    scheduler.tick(datetime(2025, 1, 1, 13, 0, 0, tzinfo=UTC))
    assert len(task_service.list_tasks()) == 2


def test_scheduler_combined_triggers(session_factory) -> None:
    worker = _default_worker()
    task_service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
    )
    config = SystemConfig(
        default_image="test",
        workspace_root="/tmp",
        scout_scheduler_enabled=True,
        scout_repo_url="https://github.com/foo/bar",
        scout_idle_trigger_minutes=30,
        scout_schedule_interval_minutes=60,
    )
    scheduler = ScoutScheduler(task_service, config)

    # Base time is 12:00
    base_time = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    scheduler._last_busy_time = base_time

    # At 13:00, BOTH idle (last busy 12:00) and schedule (rollover to 13:00) are due!
    trigger_time = datetime(2025, 1, 1, 13, 0, 0, tzinfo=UTC)
    scheduler.tick(trigger_time)

    # Should only create ONE task
    tasks = task_service.list_tasks()
    assert len(tasks) == 1
