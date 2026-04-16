"""Integration tests for the repository layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import ArtifactType, SessionStatus, TaskStatus, WorkerRunStatus, WorkerType
from db.models import PersonalMemory, ProjectMemory
from repositories import (
    ArtifactRepository,
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    TaskRepository,
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
            task_repo.heartbeat_lease(
                task_id="missing",
                worker_id="worker-a",
                now=datetime.now(UTC),
                lease_seconds=30,
            )
            is False
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
