"""Focused regression tests for queue-state helpers and persisted run context."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TaskStatus, TimelineEventType, WorkerRunStatus, WorkerRuntimeMode
from orchestrator import execution as execution_module
from repositories import (
    TaskRepository,
    TaskTimelineRepository,
    WorkerRunRepository,
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


def test_load_submission_for_task_restores_last_run_context_and_timeline_history() -> None:
    """Reloaded submissions should include last run metadata and serialized timeline events."""
    service, session_factory = _make_task_service()
    task_snapshot, persisted = service.create_task(
        execution_module.TaskSubmission(
            task_text="Recover retry context",
            repo_url="https://github.com/natanayalo/code-agent",
            branch="main",
            budget={"max_iterations": 4},
        )
    )

    event_time = datetime(2026, 3, 1, tzinfo=UTC)
    with session_scope(session_factory) as session:
        worker_run = WorkerRunRepository(session).create(
            task_id=task_snapshot.task_id,
            session_id=task_snapshot.session_id,
            worker_type="codex",
            worker_profile="codex-native-executor",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            workspace_id="workspace-ctx",
            started_at=event_time,
            finished_at=event_time,
            status="failure",
            summary="Execution failed before verifier output.",
            requested_permission="workspace_write",
            budget_usage={"iterations_used": 1},
            commands_run=[{"command": "pytest", "exit_code": 1}],
            files_changed=["broken.py"],
        )
        TaskTimelineRepository(session).create_next_for_attempt(
            task_id=task_snapshot.task_id,
            attempt_number=persisted.attempt_count,
            event_type=TimelineEventType.TASK_INGESTED,
            message="Task ingestion recorded.",
            payload={"worker_run_id": worker_run.id},
        )

    loaded = service._load_submission_for_task(task_id=task_snapshot.task_id)

    assert loaded is not None
    submission, reloaded = loaded
    assert submission.task_text == "Recover retry context"
    assert reloaded.last_run_dispatch == {
        "run_id": reloaded.last_run_dispatch["run_id"],
        "worker_type": "codex",
        "worker_profile": "codex-native-executor",
        "runtime_mode": "native_agent",
        "workspace_id": "workspace-ctx",
    }
    assert reloaded.last_run_result == {
        "status": WorkerRunStatus.FAILURE.value,
        "summary": "Execution failed before verifier output.",
        "failure_kind": "unknown",
        "workspace_id": "workspace-ctx",
        "requested_permission": "workspace_write",
        "budget_usage": {"iterations_used": 1},
        "commands_run": [{"command": "pytest", "exit_code": 1}],
        "files_changed": ["broken.py"],
        "test_results": [],
        "artifacts": [],
    }
    assert reloaded.timeline_events == [
        {
            "event_type": TimelineEventType.TASK_INGESTED.value,
            "attempt_number": persisted.attempt_count,
            "sequence_number": 0,
            "message": "Task ingestion recorded.",
            "payload": {"worker_run_id": reloaded.last_run_dispatch["run_id"]},
            "created_at": reloaded.timeline_events[0]["created_at"],
        }
    ]
    assert reloaded.timeline_events[0]["created_at"] is not None


def test_queue_state_helpers_update_retry_and_terminal_status_consistently() -> None:
    """Queue helper wrappers should preserve the expected state transitions on task rows."""
    service, session_factory = _make_task_service()

    marked_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(task_text="Mark state")
    )
    service._mark_task_in_progress(task_id=marked_snapshot.task_id)
    with session_scope(session_factory) as session:
        marked_task = TaskRepository(session).get(marked_snapshot.task_id)
        assert marked_task is not None
        assert marked_task.status is TaskStatus.IN_PROGRESS

    service._mark_task_failed(task_id=marked_snapshot.task_id)
    with session_scope(session_factory) as session:
        marked_task = TaskRepository(session).get(marked_snapshot.task_id)
        assert marked_task is not None
        assert marked_task.status is TaskStatus.FAILED

    retry_snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="Retry me"))
    retry_claim = service.claim_next_task(worker_id="worker-a", lease_seconds=30)
    assert retry_claim is not None
    assert retry_claim.task_id == retry_snapshot.task_id

    assert service._heartbeat_task_lease(
        task_id=retry_snapshot.task_id,
        worker_id="worker-a",
        lease_seconds=45,
    )
    service._record_task_attempt_error(task_id=retry_snapshot.task_id, error="x" * 5005)
    service._release_task_failure(task_id=retry_snapshot.task_id, worker_id="worker-a")

    with session_scope(session_factory) as session:
        retry_task = TaskRepository(session).get(retry_snapshot.task_id)
        assert retry_task is not None
        assert retry_task.status is TaskStatus.PENDING
        assert retry_task.lease_owner is None
        assert retry_task.lease_expires_at is None
        assert retry_task.next_attempt_at is not None
        assert retry_task.last_error == "x" * 4000

    success_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(task_text="Complete queue task")
    )
    success_claim = service.claim_next_task(worker_id="worker-b", lease_seconds=30)
    assert success_claim is not None
    assert success_claim.task_id == success_snapshot.task_id

    service._release_task_success(task_id=success_snapshot.task_id)
    with session_scope(session_factory) as session:
        success_task = TaskRepository(session).get(success_snapshot.task_id)
        assert success_task is not None
        assert success_task.status is TaskStatus.COMPLETED
        assert success_task.lease_owner is None
        assert success_task.next_attempt_at is None

    terminal_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(task_text="Stop queue task")
    )
    terminal_claim = service.claim_next_task(worker_id="worker-c", lease_seconds=30)
    assert terminal_claim is not None
    assert terminal_claim.task_id == terminal_snapshot.task_id

    service._release_task_terminal_failure(
        task_id=terminal_snapshot.task_id,
        worker_id="worker-c",
        status=TaskStatus.CANCELLED,
    )
    with session_scope(session_factory) as session:
        terminal_task = TaskRepository(session).get(terminal_snapshot.task_id)
        assert terminal_task is not None
        assert terminal_task.status is TaskStatus.CANCELLED
        assert terminal_task.lease_owner is None
        assert terminal_task.next_attempt_at is None


def test_run_queued_task_fails_retired_persisted_profile_override() -> None:
    """Queued tasks with retired persisted profile overrides should fail explicitly."""
    service, session_factory = _make_task_service()
    task_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(task_text="Reject retired profile")
    )
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_snapshot.task_id)
        assert task is not None
        task.constraints = {"worker_profile_override": "gemini-native-executor"}

    claim = service.claim_next_task(worker_id="worker-invalid-profile", lease_seconds=30)
    assert claim is not None
    assert claim.task_id == task_snapshot.task_id

    asyncio.run(
        service.run_queued_task(
            task_id=task_snapshot.task_id,
            worker_id="worker-invalid-profile",
        )
    )

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED
        assert task.lease_owner is None
        assert task.lease_expires_at is None
        assert task.next_attempt_at is None
        assert task.last_error is not None
        assert "Gemini profile names are no longer supported" in task.last_error
