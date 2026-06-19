"""Focused integration tests for inbound-delivery and worker-run persistence helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import WorkerRuntimeMode
from repositories import (
    InboundDeliveryRepository,
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
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _create_task(session) -> str:
    user = UserRepository(session).create(
        external_user_id="repo-helper-user",
        display_name="Repo Helper",
    )
    conversation_session = SessionRepository(session).create(
        user_id=user.id,
        channel="http",
        external_thread_id=f"thread-{user.id}",
    )
    task = TaskRepository(session).create(
        session_id=conversation_session.id,
        task_text="Exercise repository helper branches",
    )
    return task.id


def test_inbound_delivery_repository_only_claims_unassigned_rows(session_factory) -> None:
    """Claim only unassigned delivery rows and keep existing task assignments intact."""
    with session_scope(session_factory) as session:
        delivery_repo = InboundDeliveryRepository(session)
        task_id = _create_task(session)

        delivery_repo.create(channel="telegram", delivery_id="claimable", task_id=None)
        claimed = delivery_repo.attach_task_if_unassigned(
            channel="telegram",
            delivery_id="claimable",
            task_id=task_id,
        )
        assert claimed is not None
        assert claimed.task_id == task_id

        already_assigned = delivery_repo.create(
            channel="telegram",
            delivery_id="assigned",
            task_id="existing-task",
        )
        assert (
            delivery_repo.attach_task_if_unassigned(
                channel="telegram",
                delivery_id="assigned",
                task_id=task_id,
            )
            is None
        )
        reloaded = delivery_repo.get_by_channel_delivery(channel="telegram", delivery_id="assigned")
        assert reloaded is not None
        assert reloaded.task_id == already_assigned.task_id

        assert (
            delivery_repo.attach_task_if_unassigned(
                channel="telegram",
                delivery_id="missing",
                task_id=task_id,
            )
            is None
        )


def test_worker_run_repository_clear_artifact_index_handles_missing_and_persists_reset(
    session_factory,
) -> None:
    """Artifact index cleanup should clear persisted values and fail safely for missing runs."""
    with session_scope(session_factory) as session:
        task_id = _create_task(session)
        run_repo = WorkerRunRepository(session)
        run = run_repo.create(
            task_id=task_id,
            worker_type="codex",
            started_at=datetime.now(UTC),
            status="running",
            artifact_index=[{"name": "workspace", "uri": "/tmp/workspace"}],
        )

        cleared = run_repo.clear_artifact_index(run.id)
        assert cleared is not None
        assert cleared.artifact_index == []
        assert run_repo.get(run.id) is not None
        assert run_repo.get(run.id).artifact_index == []
        assert run_repo.clear_artifact_index("missing-run") is None


def test_worker_run_repository_complete_handles_missing_and_updates_optional_fields(
    session_factory,
) -> None:
    """Completing a run should update persisted optional fields and return None for missing rows."""
    with session_scope(session_factory) as session:
        task_id = _create_task(session)
        run_repo = WorkerRunRepository(session)
        run = run_repo.create(
            task_id=task_id,
            worker_type="antigravity",
            started_at=datetime.now(UTC),
            status="running",
        )

        completed = run_repo.complete(
            run_id=run.id,
            status="failure",
            finished_at=datetime.now(UTC),
            requested_permission="dangerous_shell",
            budget_usage={"iterations_used": 3},
            verifier_outcome={"status": "failed", "failure_kind": "review"},
            commands_run=[{"command": "pytest", "exit_code": 1}],
            files_changed_count=2,
            files_changed=["a.py", "b.py"],
            artifact_index=[{"name": "stderr.log"}],
        )

        assert completed is not None
        assert completed.requested_permission == "dangerous_shell"
        assert completed.budget_usage == {"iterations_used": 3}
        assert completed.verifier_outcome == {"status": "failed", "failure_kind": "review"}
        assert completed.commands_run == [{"command": "pytest", "exit_code": 1}]
        assert completed.files_changed_count == 2
        assert completed.files_changed == ["a.py", "b.py"]
        assert completed.artifact_index == [{"name": "stderr.log"}]
        assert (
            run_repo.complete(
                run_id="missing-run",
                status="success",
                finished_at=datetime.now(UTC),
            )
            is None
        )


def test_worker_run_repository_lists_retained_runs_and_aggregates_runtime_metrics(
    session_factory,
) -> None:
    """Retention listing and metrics should reflect runtime mode mix and legacy tool-loop usage."""
    now = datetime.now(UTC)

    with session_scope(session_factory) as session:
        task_id = _create_task(session)
        run_repo = WorkerRunRepository(session)

        oldest = run_repo.create(
            task_id=task_id,
            worker_type="codex",
            runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
            started_at=now - timedelta(minutes=30),
            finished_at=now - timedelta(minutes=29),
            retention_expires_at=now - timedelta(hours=2),
            status="success",
        )
        newer = run_repo.create(
            task_id=task_id,
            worker_type="antigravity",
            runtime_mode=WorkerRuntimeMode.TOOL_LOOP,
            started_at=now - timedelta(minutes=20),
            finished_at=now - timedelta(minutes=18),
            retention_expires_at=now - timedelta(hours=1),
            status="failure",
        )
        run_repo.create(
            task_id=task_id,
            worker_type="codex",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            started_at=now - timedelta(minutes=10),
            finished_at=now - timedelta(minutes=7),
            status="success",
        )
        run_repo.create(
            task_id=task_id,
            worker_type="openrouter",
            runtime_mode=None,
            started_at=now - timedelta(days=2),
            finished_at=now - timedelta(days=2, minutes=-1),
            status="success",
        )

        retained = run_repo.list_retained_before(now)
        assert [row.id for row in retained] == [oldest.id, newer.id]

        metrics = run_repo.get_metrics(since=now - timedelta(hours=3))
        assert metrics["worker_usage"] == {"codex": 2, "antigravity": 1}
        assert metrics["runtime_mode_usage"] == {
            WorkerRuntimeMode.TOOL_LOOP.value: 2,
            WorkerRuntimeMode.NATIVE_AGENT.value: 1,
        }
        assert metrics["legacy_tool_loop_usage"] == {"codex": 1, "antigravity": 1}
        assert metrics["avg_duration_seconds"] > 0
        assert metrics["success_rate"] == pytest.approx(2 / 3)
