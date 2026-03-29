"""Integration tests for the repository layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
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
        assert stored_task.status == "in_progress"
        assert stored_task.chosen_worker == "codex"
        assert stored_task.route_reason == "cheap_mechanical_change"
        stored_session = session_repo.get(conversation_session.id)
        assert stored_session is not None
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
            commands_run=[{"command": "pytest", "exit_code": 0}],
            files_changed_count=2,
            artifact_index=[{"name": "stdout.log"}],
        )

        stored_run = worker_run_repo.get(worker_run.id)
        assert stored_run is not None
        assert stored_run.status == "success"
        assert stored_run.summary == "Task completed"
        assert stored_run.files_changed_count == 2
        assert len(worker_run_repo.list_by_task(task.id)) == 1
        artifacts = artifact_repo.list_by_run(worker_run.id)
        assert len(artifacts) == 1
        assert artifacts[0].artifact_type == "log"


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
