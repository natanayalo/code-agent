"""Focused regression tests for task snapshot and summary mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import (
    ArtifactType,
    HumanInteractionStatus,
    HumanInteractionType,
    TimelineEventType,
    WorkerRunStatus,
)
from db.models import HumanInteraction, Task, WorkerRun
from orchestrator import execution as execution_module
from repositories import (
    ArtifactRepository,
    SessionRepository,
    TaskRepository,
    TaskTimelineRepository,
    UserRepository,
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


def test_map_task_to_summary_uses_loaded_runs_and_pending_interactions() -> None:
    """Summary mapping should use preloaded run and interaction history when available."""
    service, session_factory = _make_task_service()
    base_time = datetime(2026, 1, 1, tzinfo=UTC)

    with session_scope(session_factory) as session:
        user = UserRepository(session).create(
            external_user_id="summary-user",
            display_name="Summary User",
        )
        conversation_session = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-summary",
        )
        task = TaskRepository(session).create(
            session_id=conversation_session.id,
            task_text="Summarize task state",
            status="in_progress",
        )
        worker_run_repo = WorkerRunRepository(session)
        worker_run_repo.create(
            task_id=task.id,
            session_id=conversation_session.id,
            worker_type="codex",
            started_at=base_time,
            status="success",
            requested_permission="workspace_write",
        )
        worker_run_repo.create(
            task_id=task.id,
            session_id=conversation_session.id,
            worker_type="gemini",
            started_at=base_time + timedelta(minutes=1),
            status="failure",
            requested_permission="dangerous_shell",
        )
        session.add_all(
            [
                HumanInteraction(
                    task_id=task.id,
                    interaction_type=HumanInteractionType.CLARIFICATION,
                    status=HumanInteractionStatus.PENDING,
                    summary="Need a target file.",
                    data={"source": "test"},
                ),
                HumanInteraction(
                    task_id=task.id,
                    interaction_type=HumanInteractionType.PERMISSION,
                    status=HumanInteractionStatus.RESOLVED,
                    summary="Permission already granted.",
                    data={"source": "test"},
                    response_data={"approved": True},
                ),
            ]
        )
        session.flush()
        loaded_task = session.scalar(
            select(Task)
            .where(Task.id == task.id)
            .options(
                selectinload(Task.worker_runs),
                selectinload(Task.human_interactions),
            )
        )

        assert loaded_task is not None
        summary = service._map_task_to_summary(loaded_task)

    assert summary.latest_run_status == WorkerRunStatus.FAILURE.value
    assert summary.latest_run_worker == "gemini"
    assert summary.latest_run_requested_permission == "dangerous_shell"
    assert summary.pending_interaction_count == 1


def test_map_task_to_summary_defaults_pending_count_to_zero_without_preloaded_relationships() -> (
    None
):
    """Summary mapping should avoid implicit lazy loads when interactions were not preloaded."""
    service, session_factory = _make_task_service()

    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="no-preload", display_name="No Load")
        conversation_session = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-no-load",
        )
        task = TaskRepository(session).create(
            session_id=conversation_session.id,
            task_text="Avoid implicit lazy loads",
            status="pending",
        )
        session.add(
            HumanInteraction(
                task_id=task.id,
                interaction_type=HumanInteractionType.CLARIFICATION,
                status=HumanInteractionStatus.PENDING,
                summary="This should not be counted without preload.",
                data={"source": "test"},
            )
        )
        session.flush()

        loaded_task = session.get(Task, task.id)
        assert loaded_task is not None
        assert "human_interactions" not in loaded_task.__dict__

        summary = service._map_task_to_summary(loaded_task)

    assert summary.pending_interaction_count == 0
    assert summary.latest_run_id is None


def _setup_legacy_task(session):
    user = UserRepository(session).create(external_user_id="snapshot-user", display_name="Snap")
    conversation_session = SessionRepository(session).create(
        user_id=user.id,
        channel="http",
        external_thread_id="thread-snapshot",
    )
    return TaskRepository(session).create(
        session_id=conversation_session.id,
        task_text="Map legacy snapshot state",
        status="completed",
        task_spec="legacy-task-spec",
    ), conversation_session.id


def _setup_legacy_run(session, task_id, session_id, base_time, attempt_count):
    run = WorkerRunRepository(session).create(
        task_id=task_id,
        session_id=session_id,
        worker_type="codex",
        started_at=base_time,
        finished_at=base_time + timedelta(seconds=5),
        status="success",
        verifier_outcome={
            "status": "warning",
            "items": [
                {"label": "lint", "status": "warning"},
                {"id": "explicit-id", "label": "tests", "status": "passed"},
                "non-dict-item",
            ],
        },
        commands_run=[
            {"command": "pytest", "exit_code": 0},
            "non-dict-command",
        ],
        files_changed_count=1,
        files_changed=["note.txt"],
        artifact_index=[
            {
                "name": "stdout.log",
                "uri": "artifacts/stdout.log",
                "artifact_type": "log",
            },
            {"name": "summary"},
            "non-dict-artifact",
        ],
    )
    ArtifactRepository(session).create(
        run_id=run.id,
        artifact_type=ArtifactType.RESULT_SUMMARY,
        name="result.md",
        uri="artifacts/result.md",
        artifact_metadata={"kind": "summary"},
    )
    TaskTimelineRepository(session).create_next_for_attempt(
        task_id=task_id,
        attempt_number=attempt_count,
        event_type=TimelineEventType.TASK_COMPLETED,
        message="Task completed successfully.",
    )
    return run


def test_map_task_to_snapshot_backfills_legacy_run_ids_and_handles_legacy_task_spec() -> None:
    """Snapshot mapping should stabilize legacy run payloads without breaking old task rows."""
    service, session_factory = _make_task_service()
    base_time = datetime(2026, 2, 1, tzinfo=UTC)

    with session_scope(session_factory) as session:
        task, session_id = _setup_legacy_task(session)
        run = _setup_legacy_run(session, task.id, session_id, base_time, task.attempt_count)  # noqa: F841
        session.flush()

        loaded_task = session.scalar(
            select(Task)
            .where(Task.id == task.id)
            .options(
                selectinload(Task.worker_runs).selectinload(WorkerRun.artifacts),
                selectinload(Task.timeline_events),
            )
        )
        assert loaded_task is not None

        snapshot = service._map_task_to_snapshot(loaded_task)

    assert snapshot.task_spec is None
    assert snapshot.latest_run is not None
    assert snapshot.latest_run.commands_run == [
        {"id": "legacy-0", "command": "pytest", "exit_code": 0}
    ]
    assert snapshot.latest_run.artifact_index == [
        {
            "id": "artifacts/stdout.log",
            "name": "stdout.log",
            "uri": "artifacts/stdout.log",
            "artifact_type": "log",
        },
        {"id": "idx-1", "name": "summary"},
    ]
    assert snapshot.latest_run.verifier_outcome == {
        "status": "warning",
        "items": [
            {"id": "v-0-lint-warning", "label": "lint", "status": "warning"},
            {"id": "explicit-id", "label": "tests", "status": "passed"},
            "non-dict-item",
        ],
    }
    assert snapshot.latest_run.artifacts[0].artifact_type == ArtifactType.RESULT_SUMMARY.value
    assert snapshot.timeline[0].event_type == TimelineEventType.TASK_COMPLETED.value


def test_create_task_persists_tools_and_ignores_blank_profile_override_on_reload() -> None:
    """Submission helpers should persist tool hints while trimming blank profile overrides."""
    service, session_factory = _make_task_service()
    task_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="Run backend verification",
            worker_profile_override="   ",
            tools=["pytest", "ruff"],
        )
    )

    reloaded = service._load_submission_for_task(task_id=task_snapshot.task_id)

    assert reloaded is not None
    submission, _ = reloaded
    assert submission.worker_profile_override is None
    assert submission.tools == ["pytest", "ruff"]
    assert submission.constraints["tools"] == ["pytest", "ruff"]

    with session_scope(session_factory) as session:
        persisted_task = TaskRepository(session).get(task_snapshot.task_id)
        assert persisted_task is not None
        assert persisted_task.constraints["tools"] == ["pytest", "ruff"]
