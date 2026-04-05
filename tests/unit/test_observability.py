"""Unit tests for the T-043 structured run observability fields."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from db.base import Base, utc_now
from db.models import WorkerRun
from orchestrator.execution import TaskExecutionService
from repositories import create_engine_from_url, create_session_factory, session_scope
from workers import Worker, WorkerCommand, WorkerRequest, WorkerResult


class _MockObservabilityWorker(Worker):
    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(
            status="success",
            summary="Observability test passed.",
            requested_permission="workspace_execute",
            commands_run=[
                WorkerCommand(
                    command="ls -la",
                    exit_code=0,
                    duration_seconds=0.1,
                    output_artifact_uri="logs/stdout.log",
                )
            ],
            budget_usage={"iterations": 1, "wall_clock_seconds": 0.5},
            artifacts=[],
        )


@pytest.fixture
def session_factory():
    db_url = "sqlite:///test_observability.sqlite"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    yield factory
    Base.metadata.drop_all(engine)
    if os.path.exists("test_observability.sqlite"):
        os.remove("test_observability.sqlite")


@pytest.mark.asyncio
async def test_worker_run_observability_persistence(session_factory):
    # Setup
    worker = _MockObservabilityWorker()
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
    )

    # 1. Create a task first
    with session_scope(session_factory) as session:
        from repositories import SessionRepository, TaskRepository, UserRepository

        user_repo = UserRepository(session)
        user = user_repo.create(external_user_id="test_user")
        session_repo = SessionRepository(session)
        conv_session = session_repo.create(
            user_id=user.id, channel="test", external_thread_id="thread_1"
        )
        task_repo = TaskRepository(session)
        task = task_repo.create(session_id=conv_session.id, task_text="test task")
        task_id = task.id
        session_id = conv_session.id

    # 2. Simulate orchestrator state
    from orchestrator.state import (
        OrchestratorState,
        RouteDecision,
        SessionRef,
        TaskRequest,
        WorkerDispatch,
    )

    state = OrchestratorState(
        session=SessionRef(
            session_id=session_id,
            user_id="test_user",
            channel="test",
            external_thread_id="thread_1",
        ),
        task=TaskRequest(task_id=task_id, task_text="test task"),
        dispatch=WorkerDispatch(worker_type="codex", run_id="run_1"),
        result=await worker.run(WorkerRequest(task_text="test task")),
        route=RouteDecision(chosen_worker="codex", route_reason="observability test"),
    )

    # 3. Persist
    started_at = utc_now()
    finished_at = utc_now()
    service._persist_execution_outcome(
        task_id=task_id, state=state, started_at=started_at, finished_at=finished_at
    )

    # 4. Verify
    with session_scope(session_factory) as session:
        stmt = select(WorkerRun).where(WorkerRun.task_id == task_id)
        run = session.execute(stmt).scalar_one()

        assert run.session_id == session_id
        assert run.requested_permission == "workspace_execute"
        assert run.budget_usage == {"iterations": 1, "wall_clock_seconds": 0.5}
        assert run.commands_run[0]["command"] == "ls -la"
        assert run.commands_run[0]["output_artifact_uri"] == "logs/stdout.log"

    # 5. Verify snapshot
    snapshot = service.get_task(task_id)
    assert snapshot.latest_run.requested_permission == "workspace_execute"
    assert snapshot.latest_run.budget_usage == {"iterations": 1, "wall_clock_seconds": 0.5}
    assert snapshot.latest_run.commands_run[0]["output_artifact_uri"] == "logs/stdout.log"
