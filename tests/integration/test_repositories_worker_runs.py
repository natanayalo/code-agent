"""Integration tests for worker-run and artifact repositories."""

from __future__ import annotations

from datetime import UTC, datetime

from db.enums import ArtifactType, WorkerRunStatus, WorkerRuntimeMode
from repositories import (
    ArtifactRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    WorkerRunRepository,
    session_scope,
)


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
