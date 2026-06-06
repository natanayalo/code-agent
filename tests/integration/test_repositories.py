"""Integration tests for the repository layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import (
    ArtifactType,
    HumanInteractionStatus,
    HumanInteractionType,
    SessionStatus,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import PersonalMemory, ProjectMemory, WorkerRun
from repositories import (
    ArtifactRepository,
    HumanInteractionRepository,
    InboundDeliveryRepository,
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    SessionStateRepository,
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
    """Create a SQLite-backed session factory for repository tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_session_and_task_repositories_support_crud(session_factory) -> None:
    """Users, sessions, and tasks can be created and updated through repositories."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        user = user_repo.create(external_user_id="telegram:123", display_name="Natan")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-1",
        )
        task = task_repo.create(
            session_id=conversation_session.id,
            task_text="Add repository layer",
            repo_url="https://github.com/natanayalo/code-agent",
            branch="master",
            worker_override="gemini",
            constraints={"requires_approval": True},
            budget={"max_iterations": 8},
        )

        session_repo.set_active_task(
            session_id=conversation_session.id,
            active_task_id=task.id,
        )
        session_repo.touch(
            session_id=conversation_session.id,
            seen_at=datetime.now(UTC),
        )
        task_repo.set_route(
            task_id=task.id,
            chosen_worker="codex",
            route_reason="cheap_mechanical_change",
        )
        task_repo.update_status(task_id=task.id, status="in_progress")

        assert user_repo.get_by_external_user_id("telegram:123") is not None
        assert user_repo.get(user.id) is not None
        assert (
            session_repo.get_by_channel_thread(
                channel="telegram",
                external_thread_id="thread-1",
            )
            is not None
        )
        stored_task = task_repo.get(task.id)
        assert stored_task is not None
        assert stored_task.status is TaskStatus.IN_PROGRESS
        assert stored_task.chosen_worker is WorkerType.CODEX
        assert stored_task.route_reason == "cheap_mechanical_change"
        assert stored_task.worker_override is WorkerType.GEMINI
        assert stored_task.constraints == {"requires_approval": True}
        assert stored_task.budget == {"max_iterations": 8}
        stored_session = session_repo.get(conversation_session.id)
        assert stored_session is not None
        assert stored_session.status is SessionStatus.ACTIVE
        assert stored_session.active_task_id == task.id
        assert len(task_repo.list_by_session(conversation_session.id)) == 1


def test_session_repositories_handle_missing_rows_and_session_state_merges(session_factory) -> None:
    """Session repositories should fail safely on missing rows and merge working context updates."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        state_repo = SessionStateRepository(session)

        assert session_repo.set_active_task(session_id="missing", active_task_id="task-1") is None
        assert session_repo.touch(session_id="missing", seen_at=datetime.now(UTC)) is None

        user = user_repo.create(external_user_id="telegram:state", display_name="Stateful")
        first = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-a",
        )
        second = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-b",
        )

        state_repo.upsert(
            session_id=first.id,
            active_goal="stabilize task execution",
            decisions_made={"worker": "codex"},
            files_touched=["workers/native_agent_runner.py"],
        )
        merged = state_repo.upsert(
            session_id=first.id,
            identified_risks={"timeouts": "watch"},
            files_touched=["workers/native_agent_runner.py", "repositories/sqlalchemy.py"],
        )

        assert [row.id for row in session_repo.list_by_user(user.id)] == [first.id, second.id]
        assert merged.decisions_made == {"worker": "codex"}
        assert merged.identified_risks == {"timeouts": "watch"}
        assert merged.files_touched == [
            "workers/native_agent_runner.py",
            "repositories/sqlalchemy.py",
        ]


def test_worker_run_and_artifact_repositories_support_crud(session_factory) -> None:
    """Worker runs and artifacts can be created and completed through repositories."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        worker_run_repo = WorkerRunRepository(session)
        artifact_repo = ArtifactRepository(session)

        user = user_repo.create(external_user_id="telegram:run", display_name="Runner")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-run",
        )
        task = task_repo.create(session_id=conversation_session.id, task_text="Run worker")
        worker_run = worker_run_repo.create(
            task_id=task.id,
            session_id=conversation_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status="running",
            workspace_id="workspace-1",
        )
        artifact_repo.create(
            run_id=worker_run.id,
            artifact_type="log",
            name="stdout.log",
            uri="artifacts/stdout.log",
            artifact_metadata={"kind": "stdout"},
        )
        worker_run_repo.complete(
            run_id=worker_run.id,
            status="success",
            finished_at=datetime.now(UTC),
            summary="Task completed",
            requested_permission="workspace_write",
            budget_usage={"iterations_used": 2, "wall_clock_seconds": 1.5},
            verifier_outcome={"status": "passed", "summary": "Verifier accepted output."},
            commands_run=[{"command": "pytest", "exit_code": 0}],
            files_changed_count=2,
            artifact_index=[{"name": "stdout.log"}],
        )

        stored_run = worker_run_repo.get(worker_run.id)
        assert stored_run is not None
        assert stored_run.status is WorkerRunStatus.SUCCESS
        assert stored_run.session_id == conversation_session.id
        assert stored_run.summary == "Task completed"
        assert stored_run.requested_permission == "workspace_write"
        assert stored_run.budget_usage == {"iterations_used": 2, "wall_clock_seconds": 1.5}
        assert stored_run.verifier_outcome == {
            "status": "passed",
            "summary": "Verifier accepted output.",
        }
        assert stored_run.files_changed_count == 2
        assert len(worker_run_repo.list_by_task(task.id)) == 1
        artifacts = artifact_repo.list_by_run(worker_run.id)
        assert len(artifacts) == 1
        assert artifacts[0].artifact_type is ArtifactType.LOG


def test_worker_run_complete_preserves_existing_optional_fields(session_factory) -> None:
    """Completing a run without optional fields keeps existing persisted values."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        worker_run_repo = WorkerRunRepository(session)

        user = user_repo.create(external_user_id="telegram:preserve", display_name="Preserve")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-preserve",
        )
        task = task_repo.create(
            session_id=conversation_session.id,
            task_text="Preserve run fields",
        )
        worker_run = worker_run_repo.create(
            task_id=task.id,
            session_id=conversation_session.id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status="running",
            summary="Keep this summary",
            requested_permission="workspace_write",
            budget_usage={"iterations_used": 1},
            verifier_outcome={"status": "warning"},
            commands_run=[{"command": "pytest", "exit_code": 0}],
            artifact_index=[{"name": "stdout.log"}],
        )

        worker_run_repo.complete(
            run_id=worker_run.id,
            status="success",
            finished_at=datetime.now(UTC),
            files_changed_count=1,
        )

        stored_run = worker_run_repo.get(worker_run.id)
        assert stored_run is not None
        assert stored_run.status is WorkerRunStatus.SUCCESS
        assert stored_run.summary == "Keep this summary"
        assert stored_run.requested_permission == "workspace_write"
        assert stored_run.budget_usage == {"iterations_used": 1}
        assert stored_run.verifier_outcome == {"status": "warning"}
        assert stored_run.commands_run == [{"command": "pytest", "exit_code": 0}]
        assert stored_run.artifact_index == [{"name": "stdout.log"}]


def test_memory_repositories_support_upsert_and_delete(session_factory) -> None:
    """Personal and project memory entries support CRUD operations."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        personal_memory_repo = PersonalMemoryRepository(session)
        project_memory_repo = ProjectMemoryRepository(session)

        user = user_repo.create(external_user_id="telegram:mem", display_name="Memory User")
        personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="communication_preferences",
            value={"style": "concise"},
        )
        personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="communication_preferences",
            value={"style": "direct"},
        )
        project_memory_repo.upsert(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="known_pitfalls",
            value={"docker": "use cert.pem when needed"},
        )

        personal_memory = personal_memory_repo.get(
            user_id=user.id,
            memory_key="communication_preferences",
        )
        project_memory = project_memory_repo.get(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="known_pitfalls",
        )

        assert personal_memory is not None
        assert personal_memory.value == {"style": "direct"}
        assert project_memory is not None
        assert project_memory.value == {"docker": "use cert.pem when needed"}
        assert len(personal_memory_repo.list_by_user(user.id)) == 1
        assert (
            len(project_memory_repo.list_by_repo("https://github.com/natanayalo/code-agent")) == 1
        )
        assert personal_memory_repo.delete(
            user_id=user.id,
            memory_key="communication_preferences",
        )
        assert project_memory_repo.delete(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="known_pitfalls",
        )


def test_memory_repositories_list_all_and_delete_missing_rows(session_factory) -> None:
    """Memory listing filters and delete-miss paths should be stable for operator UIs."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        personal_memory_repo = PersonalMemoryRepository(session)
        project_memory_repo = ProjectMemoryRepository(session)

        user = user_repo.create(external_user_id="telegram:mem-list", display_name="Memory List")
        personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="editor",
            value={"theme": "light"},
        )
        personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="shell",
            value={"name": "zsh"},
        )
        project_memory_repo.upsert(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="build",
            value={"cmd": "pytest"},
        )
        project_memory_repo.upsert(
            repo_url="https://github.com/natanayalo/other",
            memory_key="build",
            value={"cmd": "npm test"},
        )

        assert len(personal_memory_repo.list_all(user_id=user.id, limit=10, offset=0)) == 2
        assert len(personal_memory_repo.list_all(limit=1, offset=1)) == 1
        assert (
            len(
                project_memory_repo.list_all(
                    repo_url="https://github.com/natanayalo/code-agent",
                    limit=10,
                    offset=0,
                )
            )
            == 1
        )
        assert (
            project_memory_repo.delete(
                repo_url="https://github.com/natanayalo/missing",
                memory_key="none",
            )
            is False
        )
        assert personal_memory_repo.delete(user_id=user.id, memory_key="missing") is False


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

        # Create some pending interactions
        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={"requires_clarification": True, "requires_permission": True},
        )

        # First cancellation
        cancelled, was_cancelled = task_repo.cancel(task_id=task.id)
        assert was_cancelled is True
        assert cancelled.status is TaskStatus.FAILED
        assert cancelled.last_error == "Task cancelled by operator."

        # Verify interactions are cancelled
        interactions = interaction_repo.list_by_task(task_id=task.id)
        assert len(interactions) == 2
        for interaction in interactions:
            assert interaction.status is HumanInteractionStatus.CANCELLED

        # Second cancellation (idempotency/terminality)
        # Mark as completed to test that it stays completed
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

        # Prime the session identity map so claim_next cannot rely on stale in-memory state.
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


def test_human_interaction_repository_syncs_task_spec_flags(session_factory) -> None:
    """TaskSpec clarification/permission flags should map to resumable pending interactions."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(
            external_user_id="telegram:interactions", display_name="Interactions"
        )
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-interactions",
        )
        task = task_repo.create(
            session_id=conversation_session.id, task_text="debug this and drop table"
        )

        task_spec = {
            "goal": "debug this and drop table",
            "requires_clarification": True,
            "requires_permission": True,
            "permission_reason": "Task is classified as high risk.",
            "risk_level": "high",
        }
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=task_spec)
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=task_spec)

        interactions = interaction_repo.list_by_task(task_id=task.id)
        assert len(interactions) == 2
        assert {interaction.interaction_type for interaction in interactions} == {
            HumanInteractionType.CLARIFICATION,
            HumanInteractionType.PERMISSION,
        }
        for interaction in interactions:
            assert interaction.status is HumanInteractionStatus.PENDING
            assert interaction.data["source"] == "task_spec"
            assert interaction.data["resume_token"].endswith(task.id)
        clarification = next(
            interaction
            for interaction in interactions
            if interaction.interaction_type is HumanInteractionType.CLARIFICATION
        )
        assert clarification.data["questions"] == [
            "What exact repo, files, behavior, or failure should the worker target for: "
            "debug this and drop table?"
        ]

        clarification.status = HumanInteractionStatus.RESOLVED
        session.flush()
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=task_spec)
        after_resync = interaction_repo.list_by_task(task_id=task.id)
        assert len(after_resync) == 2
        clarification_rows = [
            interaction
            for interaction in after_resync
            if interaction.interaction_type is HumanInteractionType.CLARIFICATION
        ]
        assert len(clarification_rows) == 1
        assert clarification_rows[0].status is HumanInteractionStatus.RESOLVED

        changed_task_spec = {
            **task_spec,
            "clarification_questions": ["Which migration file should be updated?"],
        }
        interaction_repo.sync_task_spec_flags(task_id=task.id, task_spec=changed_task_spec)
        after_changed_spec = interaction_repo.list_by_task(task_id=task.id)
        changed_clarification_rows = [
            interaction
            for interaction in after_changed_spec
            if interaction.interaction_type is HumanInteractionType.CLARIFICATION
        ]
        assert len(changed_clarification_rows) == 1
        assert changed_clarification_rows[0].status is HumanInteractionStatus.RESOLVED

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={"requires_clarification": False, "requires_permission": False},
        )
        refreshed = interaction_repo.list_by_task(task_id=task.id)
        assert len(refreshed) == 2
        assert any(
            interaction.status is HumanInteractionStatus.RESOLVED for interaction in refreshed
        )
        assert any(
            interaction.interaction_type is HumanInteractionType.PERMISSION
            and interaction.status is HumanInteractionStatus.CANCELLED
            for interaction in refreshed
        )


def test_human_interaction_repository_filters_statuses_and_reopens_materially_changed_checkpoints(
    session_factory,
) -> None:
    """Interaction listing filters and material changes should create fresh pending checkpoints."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(
            external_user_id="telegram:interaction-filter",
            display_name="Filter",
        )
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-filter",
        )
        task = task_repo.create(session_id=conversation_session.id, task_text="needs clarification")

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={
                "goal": "needs clarification",
                "requires_clarification": True,
                "clarification_questions": ["Which file should change?"],
            },
        )
        initial = interaction_repo.list_by_task(
            task_id=task.id,
            interaction_types=(HumanInteractionType.CLARIFICATION,),
            statuses=(HumanInteractionStatus.PENDING,),
        )
        assert len(initial) == 1
        initial[0].status = HumanInteractionStatus.RESOLVED
        initial[0].data = {
            "source": "task_spec",
            "resume_token": "clarification-other-task",
            "questions": ["Which file should change?"],
        }
        initial[0].response_data = {"answer": "main.py"}
        session.flush()

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={
                "goal": "needs clarification",
                "requires_clarification": True,
                "clarification_questions": ["Which test should be updated too?"],
            },
        )

        pending = interaction_repo.list_by_task(
            task_id=task.id,
            interaction_types=(HumanInteractionType.CLARIFICATION,),
            statuses=(HumanInteractionStatus.PENDING,),
        )
        assert len(pending) == 1
        assert pending[0].data["questions"] == ["Which test should be updated too?"]


def test_human_interaction_repository_collapses_duplicate_pending_rows(session_factory) -> None:
    """Syncing TaskSpec flags should keep one live pending row and cancel stale duplicates."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        interaction_repo = HumanInteractionRepository(session)

        user = user_repo.create(
            external_user_id="telegram:interaction-dupes",
            display_name="Dupes",
        )
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-dupes",
        )
        task = task_repo.create(
            session_id=conversation_session.id,
            task_text="duplicate pending rows",
        )

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={"goal": "duplicate pending rows", "requires_clarification": True},
        )
        first = interaction_repo.list_by_task(task_id=task.id)[0]
        interaction_repo.session.add(
            first.__class__(
                task_id=task.id,
                interaction_type=first.interaction_type,
                status=HumanInteractionStatus.PENDING,
                summary="stale duplicate",
                data={
                    "source": "task_spec",
                    "resume_token": f"clarification-{task.id}",
                    "questions": ["stale"],
                },
            )
        )
        session.flush()

        interaction_repo.sync_task_spec_flags(
            task_id=task.id,
            task_spec={"goal": "duplicate pending rows", "requires_clarification": True},
        )

        rows = interaction_repo.list_by_task(task_id=task.id)
        clarification_rows = [
            row for row in rows if row.interaction_type is HumanInteractionType.CLARIFICATION
        ]
        assert len(clarification_rows) == 2
        assert sum(row.status is HumanInteractionStatus.PENDING for row in clarification_rows) == 1
        assert (
            sum(row.status is HumanInteractionStatus.CANCELLED for row in clarification_rows) == 1
        )


def test_personal_memory_upsert_recovers_from_duplicate_insert_race(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate insert race updates the existing personal memory entry."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        personal_memory_repo = PersonalMemoryRepository(session)

        user = user_repo.create(external_user_id="telegram:race", display_name="Race User")
        existing_entry = PersonalMemory(
            user_id=user.id,
            memory_key="communication_preferences",
            value={"style": "concise"},
        )
        session.add(existing_entry)
        session.flush()

        original_get = personal_memory_repo.get
        get_calls = 0

        def stale_get(*, user_id: str, memory_key: str) -> PersonalMemory | None:
            nonlocal get_calls
            get_calls += 1
            if get_calls == 1:
                return None
            return original_get(user_id=user_id, memory_key=memory_key)

        monkeypatch.setattr(personal_memory_repo, "get", stale_get)

        updated_entry = personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="communication_preferences",
            value={"style": "direct"},
        )

        stored_entry = original_get(
            user_id=user.id,
            memory_key="communication_preferences",
        )
        assert updated_entry.id == existing_entry.id
        assert stored_entry is not None
        assert stored_entry.value == {"style": "direct"}


def test_project_memory_upsert_recovers_from_duplicate_insert_race(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate insert race updates the existing project memory entry."""
    with session_scope(session_factory) as session:
        project_memory_repo = ProjectMemoryRepository(session)
        repo_url = "https://github.com/natanayalo/code-agent"

        existing_entry = ProjectMemory(
            repo_url=repo_url,
            memory_key="known_pitfalls",
            value={"docker": "use cert.pem when needed"},
        )
        session.add(existing_entry)
        session.flush()

        original_get = project_memory_repo.get
        get_calls = 0

        def stale_get(*, repo_url: str, memory_key: str) -> ProjectMemory | None:
            nonlocal get_calls
            get_calls += 1
            if get_calls == 1:
                return None
            return original_get(repo_url=repo_url, memory_key=memory_key)

        monkeypatch.setattr(project_memory_repo, "get", stale_get)

        updated_entry = project_memory_repo.upsert(
            repo_url=repo_url,
            memory_key="known_pitfalls",
            value={"docker": "updated after retry"},
        )

        stored_entry = original_get(
            repo_url=repo_url,
            memory_key="known_pitfalls",
        )
        assert updated_entry.id == existing_entry.id
        assert stored_entry is not None
        assert stored_entry.value == {"docker": "updated after retry"}


def test_inbound_delivery_repository_attaches_tasks_once(session_factory) -> None:
    """Inbound delivery dedupe claims should attach a task only to unassigned rows."""
    with session_scope(session_factory) as session:
        delivery_repo = InboundDeliveryRepository(session)

        created = delivery_repo.create(channel="telegram", delivery_id="delivery-1")
        assert created.task_id is None
        fetched = delivery_repo.get_by_channel_delivery(
            channel="telegram",
            delivery_id="delivery-1",
        )
        assert fetched is not None
        assert fetched.id == created.id

        attached = delivery_repo.attach_task_if_unassigned(
            channel="telegram",
            delivery_id="delivery-1",
            task_id="task-1",
        )
        assert attached is not None
        assert attached.task_id == "task-1"
        assert (
            delivery_repo.attach_task_if_unassigned(
                channel="telegram",
                delivery_id="delivery-1",
                task_id="task-2",
            )
            is None
        )
        assert (
            delivery_repo.get_by_channel_delivery(channel="telegram", delivery_id="missing") is None
        )


def test_repository_listing_with_pagination(session_factory) -> None:
    """Repositories should support listing all records with pagination and filtering."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        user = user_repo.create(external_user_id="list:user", display_name="List User")

        # Create multiple sessions
        sessions = []
        for i in range(10):
            s = session_repo.create(
                user_id=user.id,
                channel="http",
                external_thread_id=f"thread-{i}",
            )
            sessions.append(s)

            # Create a task for each session
            task_repo.create(
                session_id=s.id,
                task_text=f"task {i}",
                status=TaskStatus.COMPLETED if i % 2 == 0 else TaskStatus.FAILED,
            )

        # Test session listing
        all_sessions = session_repo.list_all(limit=5, offset=0)
        assert len(all_sessions) == 5
        # Should be ordered by created_at desc
        assert all_sessions[0].external_thread_id == "thread-9"

        second_page_sessions = session_repo.list_all(limit=5, offset=5)
        assert len(second_page_sessions) == 5
        assert second_page_sessions[0].external_thread_id == "thread-4"

        # Test task listing
        all_tasks = task_repo.list_all(limit=5, offset=0)
        assert len(all_tasks) == 5
        # Should be ordered by created_at desc
        assert all_tasks[0].task_text == "task 9"

        # Test task filtering by session
        session_tasks = task_repo.list_all(session_id=sessions[0].id)
        assert len(session_tasks) == 1
        assert session_tasks[0].task_text == "task 0"

        # Test task filtering by status
        completed_tasks = task_repo.list_all(status=TaskStatus.COMPLETED)
        assert len(completed_tasks) == 5
        for t in completed_tasks:
            assert t.status is TaskStatus.COMPLETED


def test_worker_run_repositories_support_expiry_artifact_cleanup_and_metrics(
    session_factory,
) -> None:
    """Worker run repositories should expose retention cleanup state and runtime metrics."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)
        worker_run_repo = WorkerRunRepository(session)
        artifact_repo = ArtifactRepository(session)

        user = user_repo.create(external_user_id="telegram:run-metrics", display_name="Runs")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-run-metrics",
        )
        task = task_repo.create(session_id=conversation_session.id, task_text="run metrics")
        first_run = worker_run_repo.create(
            task_id=task.id,
            session_id=conversation_session.id,
            worker_type="codex",
            started_at=datetime(2026, 1, 2, tzinfo=UTC),
            status="running",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            retention_expires_at=datetime(2026, 1, 3, tzinfo=UTC),
            artifact_index=[{"name": "stdout.log"}],
        )
        second_run = worker_run_repo.create(
            task_id=task.id,
            session_id=conversation_session.id,
            worker_type="gemini",
            started_at=datetime(2026, 1, 4, tzinfo=UTC),
            status="running",
            runtime_mode=None,
            retention_expires_at=datetime(2026, 1, 5, tzinfo=UTC),
        )
        artifact_repo.create(
            run_id=first_run.id,
            artifact_type="workspace",
            name="workspace",
            uri="/tmp/workspace",
        )

        assert (
            worker_run_repo.complete(
                run_id="missing",
                status="failure",
                finished_at=datetime.now(UTC),
            )
            is None
        )
        assert worker_run_repo.clear_artifact_index("missing") is None

        worker_run_repo.complete(
            run_id=first_run.id,
            status="success",
            finished_at=datetime(2026, 1, 2, 0, 0, 5, tzinfo=UTC),
            files_changed=["a.py", "b.py"],
            files_changed_count=2,
        )
        worker_run_repo.complete(
            run_id=second_run.id,
            status="failure",
            finished_at=datetime(2026, 1, 4, 0, 0, 10, tzinfo=UTC),
        )

        cleared = worker_run_repo.clear_artifact_index(first_run.id)
        assert cleared is not None
        assert cleared.artifact_index == []
        assert artifact_repo.delete_by_run("missing-run") == 0
        assert artifact_repo.delete_by_run(first_run.id) == 1

        expired = worker_run_repo.list_retained_before(
            retention_expires_before=datetime(2026, 1, 4, tzinfo=UTC)
        )
        assert [row.id for row in expired] == [first_run.id]

        metrics = worker_run_repo.get_metrics(since=datetime(2026, 1, 1, tzinfo=UTC))
        assert metrics["worker_usage"] == {"codex": 1, "gemini": 1}
        assert metrics["runtime_mode_usage"] == {"native_agent": 1, "unknown": 1}
        assert metrics["legacy_tool_loop_usage"] == {}
        assert metrics["avg_duration_seconds"] == 7.5
        assert metrics["success_rate"] == 0.5

        empty_metrics = worker_run_repo.get_metrics(since=datetime(2027, 1, 1, tzinfo=UTC))
        assert empty_metrics["worker_usage"] == {}
        assert empty_metrics["runtime_mode_usage"] == {}
        assert empty_metrics["legacy_tool_loop_usage"] == {}
        assert empty_metrics["avg_duration_seconds"] == 0.0
        assert empty_metrics["success_rate"] == 0


def test_task_listing_uses_run_id_tie_breaker_for_latest_run(session_factory) -> None:
    """Listing should deterministically choose the latest run when started_at ties."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        user = user_repo.create(external_user_id="list:tie", display_name="Tie Breaker")
        conversation_session = session_repo.create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-tie",
        )
        task = task_repo.create(
            session_id=conversation_session.id,
            task_text="task with tied run timestamps",
        )

        tied_started_at = datetime(2026, 1, 1, tzinfo=UTC)
        lower_id = "00000000-0000-0000-0000-000000000001"
        higher_id = "00000000-0000-0000-0000-000000000002"
        session.add_all(
            [
                WorkerRun(
                    id=lower_id,
                    task_id=task.id,
                    session_id=conversation_session.id,
                    worker_type=WorkerType.CODEX,
                    started_at=tied_started_at,
                    status=WorkerRunStatus.RUNNING,
                    requested_permission="workspace_read",
                ),
                WorkerRun(
                    id=higher_id,
                    task_id=task.id,
                    session_id=conversation_session.id,
                    worker_type=WorkerType.GEMINI,
                    started_at=tied_started_at,
                    status=WorkerRunStatus.FAILURE,
                    requested_permission="workspace_write",
                ),
            ]
        )
        session.flush()

        listed_tasks = task_repo.list_all(limit=10, offset=0, preload_history=False)
        listed_task = next(row for row in listed_tasks if row.id == task.id)

        assert getattr(listed_task, "_latest_run_id") == higher_id
        assert getattr(listed_task, "_latest_run_worker") is WorkerType.GEMINI
        assert getattr(listed_task, "_latest_run_status") is WorkerRunStatus.FAILURE
        assert getattr(listed_task, "_latest_run_requested_permission") == "workspace_write"


def test_task_timeline_repository_supports_batch_creation(session_factory) -> None:
    """Timeline batch creation should preserve provided timestamps and ignore empty batches."""
    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        timeline_repo = TaskTimelineRepository(session)
        task = task_repo.create(session_id="session-timeline", task_text="timeline batch")

        timeline_repo.create_batch(task_id=task.id, events=[])
        created_at = datetime(2026, 1, 1, tzinfo=UTC)
        timeline_repo.create_batch(
            task_id=task.id,
            events=[
                {
                    "attempt_number": 0,
                    "sequence_number": 0,
                    "event_type": TimelineEventType.TASK_INGESTED,
                    "message": "ingested",
                    "created_at": created_at,
                },
                {
                    "attempt_number": 0,
                    "sequence_number": 1,
                    "event_type": TimelineEventType.WORKER_SELECTED,
                    "message": "worker selected",
                },
            ],
        )

        events = timeline_repo.list_by_task(task.id)
        assert len(events) == 2
        assert events[0].created_at == created_at.replace(tzinfo=None)
        assert events[0].updated_at == created_at.replace(tzinfo=None)
        assert events[1].message == "worker selected"
