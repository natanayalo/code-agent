"""Integration tests for session and task repository basics."""

from __future__ import annotations

from datetime import UTC, datetime

from db.enums import SessionStatus, TaskStatus, WorkerRunStatus, WorkerType
from db.models import WorkerRun
from repositories import (
    SessionRepository,
    SessionStateRepository,
    TaskRepository,
    UserRepository,
    session_scope,
)


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
            worker_override="antigravity",
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
        assert stored_task.worker_override is WorkerType.ANTIGRAVITY
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


def test_repository_listing_with_pagination(session_factory) -> None:
    """Repositories should support listing all records with pagination and filtering."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        user = user_repo.create(external_user_id="list:user", display_name="List User")

        sessions = []
        for i in range(10):
            row = session_repo.create(
                user_id=user.id,
                channel="http",
                external_thread_id=f"thread-{i}",
            )
            sessions.append(row)
            task_repo.create(
                session_id=row.id,
                task_text=f"task {i}",
                status=TaskStatus.COMPLETED if i % 2 == 0 else TaskStatus.FAILED,
            )

        all_sessions = session_repo.list_all(limit=5, offset=0)
        assert len(all_sessions) == 5
        assert all_sessions[0].external_thread_id == "thread-9"

        second_page_sessions = session_repo.list_all(limit=5, offset=5)
        assert len(second_page_sessions) == 5
        assert second_page_sessions[0].external_thread_id == "thread-4"

        all_tasks = task_repo.list_all(limit=5, offset=0)
        assert len(all_tasks) == 5
        assert all_tasks[0].task_text == "task 9"

        session_tasks = task_repo.list_all(session_id=sessions[0].id)
        assert len(session_tasks) == 1
        assert session_tasks[0].task_text == "task 0"

        completed_tasks = task_repo.list_all(status=TaskStatus.COMPLETED)
        assert len(completed_tasks) == 5
        for row in completed_tasks:
            assert row.status is TaskStatus.COMPLETED


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
                    worker_type=WorkerType.ANTIGRAVITY,
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
        assert getattr(listed_task, "_latest_run_worker") is WorkerType.ANTIGRAVITY
        assert getattr(listed_task, "_latest_run_status") is WorkerRunStatus.FAILURE
        assert getattr(listed_task, "_latest_run_requested_permission") == "workspace_write"
