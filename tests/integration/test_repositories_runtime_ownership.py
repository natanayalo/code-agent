"""Integration coverage for legacy queue runtime ownership boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

from db.enums import OrchestrationRuntime, TaskStatus
from repositories import (
    SessionRepository,
    TaskRepository,
    UserRepository,
    WorkerNodeRepository,
    session_scope,
)


def test_reclaim_expired_leases_only_mutates_legacy_tasks(session_factory) -> None:
    """Legacy maintenance must not alter Temporal or unknown task leases."""
    now = datetime.now(UTC)
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(
            external_user_id="telegram:runtime-reclaim", display_name="Runtime reclaim"
        )
        conversation_session = SessionRepository(session).create(
            user_id=user.id, channel="telegram", external_thread_id="runtime-reclaim-thread"
        )
        task_repo = TaskRepository(session)
        worker = WorkerNodeRepository(session).register_worker(
            worker_id="worker-runtime-reclaim", worker_type="codex", now=now, capacity=5
        )
        tasks = [
            task_repo.create(
                session_id=conversation_session.id,
                task_text=task_text,
                orchestration_runtime=runtime,
            )
            for task_text, runtime in (
                ("legacy expired", OrchestrationRuntime.LEGACY),
                ("temporal expired", OrchestrationRuntime.TEMPORAL),
                ("unknown expired", None),
                ("legacy active", OrchestrationRuntime.LEGACY),
                ("temporal active", OrchestrationRuntime.TEMPORAL),
            )
        ]
        for task, expiry in zip(
            tasks,
            (now - datetime.resolution,) * 3 + (now + datetime.resolution,) * 2,
            strict=True,
        ):
            task.status = TaskStatus.IN_PROGRESS
            task.lease_owner = worker.worker_id
            task.lease_expires_at = expiry
        worker.current_load = 5
        session.flush()

        assert task_repo.reclaim_expired_leases(now=now) == 1
        for task in tasks:
            session.refresh(task)
        session.refresh(worker)

        assert tasks[0].status is TaskStatus.PENDING
        assert tasks[0].lease_owner is None
        assert tasks[1].status is TaskStatus.IN_PROGRESS
        assert tasks[2].status is TaskStatus.IN_PROGRESS
        assert worker.current_load == 1


def test_ownership_release_restores_unexecuted_attempt(session_factory) -> None:
    """Rejecting an accidental non-legacy claim must not consume an attempt."""
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(
            external_user_id="telegram:runtime-release", display_name="Runtime release"
        )
        conversation_session = SessionRepository(session).create(
            user_id=user.id, channel="telegram", external_thread_id="runtime-release-thread"
        )
        task = TaskRepository(session).create(
            session_id=conversation_session.id,
            task_text="do not execute",
            orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        )
        task.status = TaskStatus.IN_PROGRESS
        task.lease_owner = "worker-runtime-release"
        task.lease_expires_at = datetime.now(UTC)
        task.attempt_count = 1
        session.flush()

        released = TaskRepository(session).release_runtime_ownership_violation(
            task_id=task.id, worker_id="worker-runtime-release"
        )
        session.refresh(task)

        assert released is True
        assert task.status is TaskStatus.PENDING
        assert task.attempt_count == 0
        assert task.lease_owner is None


def test_legacy_maintenance_ignores_nonlegacy_leases(session_factory) -> None:
    """Legacy heartbeat and release helpers must preserve non-legacy task state."""
    now = datetime.now(UTC)
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(
            external_user_id="telegram:runtime-maintenance", display_name="Runtime maintenance"
        )
        conversation_session = SessionRepository(session).create(
            user_id=user.id, channel="telegram", external_thread_id="runtime-maintenance-thread"
        )
        task_repo = TaskRepository(session)
        tasks = [
            task_repo.create(
                session_id=conversation_session.id,
                task_text=f"nonlegacy {index}",
                orchestration_runtime=OrchestrationRuntime.TEMPORAL,
            )
            for index in range(3)
        ]
        for task in tasks:
            task.status = TaskStatus.IN_PROGRESS
            task.lease_owner = "worker-runtime-maintenance"
            task.lease_expires_at = now
        session.flush()

        assert not task_repo.heartbeat_lease(
            task_id=tasks[0].id,
            worker_id="worker-runtime-maintenance",
            now=now,
            lease_seconds=30,
        )
        task_repo.release_success(task_id=tasks[0].id, worker_id="worker-runtime-maintenance")
        task_repo.release_failure(
            task_id=tasks[1].id,
            worker_id="worker-runtime-maintenance",
            now=now,
            retry_backoff_seconds=0,
        )
        task_repo.release_terminal_failure(
            task_id=tasks[2].id, worker_id="worker-runtime-maintenance"
        )

        for task in tasks:
            session.refresh(task)
            assert task.status is TaskStatus.IN_PROGRESS
            assert task.lease_owner == "worker-runtime-maintenance"
