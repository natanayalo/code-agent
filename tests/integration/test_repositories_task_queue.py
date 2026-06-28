"""Integration tests for task queue and task control repository behavior."""

from __future__ import annotations

from datetime import UTC, datetime

from db.enums import HumanInteractionStatus, TaskStatus
from repositories import (
    HumanInteractionRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    WorkerNodeRepository,
    session_scope,
)


def test_task_repository_release_terminal_failure_clears_lease(session_factory) -> None:
    """Terminal release should mark failed, clear lease, and avoid requeue timestamps."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        user = user_repo.create(external_user_id="telegram:terminal", display_name="Terminal")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-terminal",
        )
        task = task_repo.create(
            session_id=conversation_session.id,
            task_text="Needs manual approval",
        )
        claimed = task_repo.claim_next(
            worker_id="worker-a",
            now=datetime.now(UTC),
            lease_seconds=30,
        )
        assert claimed is not None

        updated = task_repo.release_terminal_failure(task_id=task.id, worker_id="worker-a")
        assert updated is not None
        assert updated.status is TaskStatus.FAILED
        assert updated.next_attempt_at is None
        assert updated.lease_owner is None
        assert updated.lease_expires_at is None


def test_task_repository_cancel_is_atomic_and_terminal(session_factory) -> None:
    """Cancellation must be terminal, idempotent, and clean up pending interactions."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(external_user_id="telegram:cancel", display_name="Canceller")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-cancel",
        )
        task = task_repo.create(session_id=conversation_session.id, task_text="cancel me")

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={"requires_clarification": True, "requires_permission": True},
        )

        cancelled, was_cancelled = task_repo.cancel(task_id=task.id)
        assert was_cancelled is True
        assert cancelled.status is TaskStatus.FAILED
        assert cancelled.last_error == "Task cancelled by operator."

        interactions = interaction_repo.list_by_task(task_id=task.id)
        assert len(interactions) == 2
        for interaction in interactions:
            assert interaction.status is HumanInteractionStatus.CANCELLED

        cancelled.status = TaskStatus.COMPLETED
        cancelled.last_error = None
        session.flush()

        re_cancelled, re_was_cancelled = task_repo.cancel(task_id=task.id)
        assert re_was_cancelled is False
        assert re_cancelled.status is TaskStatus.COMPLETED
        assert re_cancelled.last_error is None


def test_task_repository_claim_next_returns_fresh_claimed_task(session_factory) -> None:
    """Claim should return current DB state even when task was loaded before claiming."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        user = user_repo.create(external_user_id="telegram:claim-fresh", display_name="Fresh")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-claim-fresh",
        )
        task = task_repo.create(session_id=conversation_session.id, task_text="claim me")

        primed = task_repo.get(task.id)
        assert primed is not None
        assert primed.status is TaskStatus.PENDING

        claimed = task_repo.claim_next(
            worker_id="worker-a",
            now=datetime.now(UTC),
            lease_seconds=30,
        )
        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.status is TaskStatus.IN_PROGRESS
        assert claimed.lease_owner == "worker-a"
        assert claimed.attempt_count == 1


def test_task_repository_claim_next_respects_worker_capacity(session_factory) -> None:
    """A worker cannot claim beyond its registered active capacity."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        worker_repo = WorkerNodeRepository(session)

        user = user_repo.create(external_user_id="telegram:capacity", display_name="Capacity")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-capacity",
        )
        task_repo.create(session_id=conversation_session.id, task_text="first")
        task_repo.create(session_id=conversation_session.id, task_text="second")
        worker_repo.register_worker(
            worker_id="worker-cap",
            worker_type="codex",
            now=datetime.now(UTC),
            capacity=1,
        )

        first = task_repo.claim_next(
            worker_id="worker-cap",
            now=datetime.now(UTC),
            lease_seconds=30,
        )
        second = task_repo.claim_next(
            worker_id="worker-cap",
            now=datetime.now(UTC),
            lease_seconds=30,
        )
        worker = worker_repo.get_by_worker_id("worker-cap")

        assert first is not None
        assert second is None
        assert worker is not None
        assert worker.current_load == 1


def test_task_repository_reclaim_expired_leases_rebuilds_worker_load(session_factory) -> None:
    """Expired lease reconciliation should use remaining active leases as truth."""
    now = datetime.now(UTC)
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        worker_repo = WorkerNodeRepository(session)

        user = user_repo.create(external_user_id="telegram:reclaim", display_name="Reclaim")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-reclaim",
        )
        first_task = task_repo.create(session_id=conversation_session.id, task_text="first")
        second_task = task_repo.create(session_id=conversation_session.id, task_text="second")
        worker_repo.register_worker(
            worker_id="worker-reclaim",
            worker_type="codex",
            now=now,
            capacity=2,
        )

        assert task_repo.claim_next(worker_id="worker-reclaim", now=now, lease_seconds=30)
        assert task_repo.claim_next(worker_id="worker-reclaim", now=now, lease_seconds=30)
        worker = worker_repo.get_by_worker_id("worker-reclaim")
        assert worker is not None
        assert worker.current_load == 2

        first = task_repo.get(first_task.id)
        second = task_repo.get(second_task.id)
        assert first is not None
        assert second is not None
        first.lease_expires_at = now - datetime.resolution
        second.lease_expires_at = now + datetime.resolution
        session.flush()

        with session.no_autoflush:
            reclaimed = task_repo.reclaim_expired_leases(now=now)

        assert reclaimed == 1
        assert first.status is TaskStatus.PENDING
        assert first.lease_owner is None
        assert second.status is TaskStatus.IN_PROGRESS
        assert worker.current_load == 1


def test_task_repository_queue_release_guard_paths(session_factory) -> None:
    """Queue release helpers should handle missing rows and ownership mismatches safely."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        assert task_repo.release_success(task_id="missing") is None
        assert (
            task_repo.release_failure(
                task_id="missing",
                worker_id="worker-a",
                now=datetime.now(UTC),
                retry_backoff_seconds=10,
            )
            is None
        )
        assert task_repo.release_terminal_failure(task_id="missing", worker_id="worker-a") is None
        assert task_repo.record_attempt_error(task_id="missing", error_text="boom") is None
        assert (
            task_repo.set_route(
                task_id="missing",
                chosen_worker="codex",
                route_reason="none",
            )
            is None
        )
        assert task_repo.update_status(task_id="missing", status=TaskStatus.FAILED) is None
        assert task_repo.cancel(task_id="missing") == (None, False)
        assert (
            task_repo.heartbeat_lease(
                task_id="missing",
                worker_id="worker-a",
                now=datetime.now(UTC),
                lease_seconds=30,
            )
            is False
        )
        assert (
            task_repo.claim_next(
                worker_id="worker-a",
                now=datetime.now(UTC),
                lease_seconds=30,
            )
            is None
        )

        user = user_repo.create(external_user_id="telegram:mismatch", display_name="Mismatch")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-mismatch",
        )
        task = task_repo.create(session_id=conversation_session.id, task_text="mismatch release")
        task_repo.update_status(task_id=task.id, status=TaskStatus.IN_PROGRESS)
        seeded = task_repo.get(task.id)
        assert seeded is not None
        seeded.lease_owner = "worker-a"
        returned = task_repo.release_failure(
            task_id=task.id,
            worker_id="worker-b",
            now=datetime.now(UTC),
            retry_backoff_seconds=10,
        )
        assert returned is not None
        assert returned.status is TaskStatus.IN_PROGRESS
        assert returned.lease_owner == "worker-a"


def test_task_repository_release_failure_marks_terminal_after_final_attempt(
    session_factory,
) -> None:
    """A final failed attempt should mark the task terminal instead of requeueing it."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        user = user_repo.create(external_user_id="telegram:final-failure", display_name="Final")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-final-failure",
        )
        task = task_repo.create(
            session_id=conversation_session.id,
            task_text="fail terminally",
            max_attempts=1,
        )
        claimed = task_repo.claim_next(
            worker_id="worker-a",
            now=datetime.now(UTC),
            lease_seconds=30,
        )
        assert claimed is not None

        released = task_repo.release_failure(
            task_id=task.id,
            worker_id="worker-a",
            now=datetime.now(UTC),
            retry_backoff_seconds=30,
        )

        assert released is not None
        assert released.status is TaskStatus.FAILED
        assert released.next_attempt_at is None
        assert released.lease_owner is None


def test_task_repository_supports_task_spec_lease_progress_and_metrics(session_factory) -> None:
    """Task repository should persist task specs, queue transitions, and aggregate metrics."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(external_user_id="telegram:task-metrics", display_name="Metrics")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-task-metrics",
        )
        old_task = task_repo.create(
            session_id=conversation_session.id,
            task_text="older task",
            status=TaskStatus.COMPLETED,
        )
        old_task.created_at = datetime(2025, 1, 1, tzinfo=UTC)
        old_task.updated_at = old_task.created_at

        queued_task = task_repo.create(
            session_id=conversation_session.id,
            task_text="queue task",
            max_attempts=3,
        )
        assert task_repo.set_task_spec(task_id="missing", task_spec={"goal": "missing"}) is None
        updated_spec = task_repo.set_task_spec(
            task_id=queued_task.id,
            task_spec={"goal": "queue task", "risk_level": "low"},
        )
        assert updated_spec is not None
        assert updated_spec.task_spec == {"goal": "queue task", "risk_level": "low"}

        interaction_repo.sync_task_spec_flags(
            task_id=queued_task.id,
            task_spec={"requires_clarification": True, "goal": "queue task"},
        )

        claimed = task_repo.claim_next(
            worker_id="worker-a",
            now=datetime.now(UTC),
            lease_seconds=30,
        )
        assert claimed is not None
        previous_expiry = claimed.lease_expires_at
        assert previous_expiry is not None
        assert (
            task_repo.heartbeat_lease(
                task_id=queued_task.id,
                worker_id="worker-a",
                now=datetime.now(UTC),
                lease_seconds=90,
            )
            is True
        )

        session.expire_all()
        heartbeated = task_repo.get(queued_task.id)
        assert heartbeated is not None
        assert heartbeated.lease_expires_at is not None
        assert heartbeated.lease_expires_at > previous_expiry

        task_repo.record_attempt_error(task_id=queued_task.id, error_text="x" * 5005)
        after_error = task_repo.get(queued_task.id)
        assert after_error is not None
        assert after_error.last_error == "x" * 4000

        requeued = task_repo.release_failure(
            task_id=queued_task.id,
            worker_id="worker-a",
            now=datetime.now(UTC),
            retry_backoff_seconds=0,
        )
        assert requeued is not None
        assert requeued.status is TaskStatus.PENDING
        assert requeued.next_attempt_at is not None
        assert requeued.lease_owner is None

        reclaimed = task_repo.claim_next(
            worker_id="worker-a",
            now=datetime.now(UTC),
            lease_seconds=30,
        )
        assert reclaimed is not None
        completed = task_repo.release_success(task_id=queued_task.id)
        assert completed is not None
        assert completed.status is TaskStatus.COMPLETED
        assert completed.lease_owner is None
        assert completed.lease_expires_at is None
        assert completed.next_attempt_at is None
        assert completed.last_error is None

        listed_tasks = task_repo.list_all(
            session_id=conversation_session.id,
            status="completed",
            limit=10,
            offset=0,
            preload_history=False,
        )
        listed = next(row for row in listed_tasks if row.id == queued_task.id)
        assert getattr(listed, "_pending_interaction_count") == 1

        metrics = task_repo.get_metrics(since=datetime(2026, 1, 1, tzinfo=UTC))
        assert metrics["status_counts"]["completed"] == 1
        assert metrics["total_tasks"] == 1
        assert metrics["retried_tasks"] == 1
        assert metrics["retry_rate"] == 1
