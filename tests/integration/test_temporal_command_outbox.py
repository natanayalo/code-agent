"""Dedicated integration coverage for durable Postgres-to-Temporal handoff."""

from __future__ import annotations

import asyncio

from sqlalchemy.pool import StaticPool

from db.base import Base
from db.models import TemporalCommand
from orchestrator.execution import TaskExecutionService, TaskSubmission
from orchestrator.temporal.command_dispatcher import TemporalCommandDispatcher
from repositories import create_engine_from_url, create_session_factory, session_scope
from workers import Worker, WorkerRequest, WorkerResult


class _Worker(Worker):
    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary="unused")


class _TemporalClient:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start_workflow(self, _name, task_id, **_kwargs) -> None:
        self.started.append(task_id)


def test_temporal_start_command_survives_submission_until_dispatch(monkeypatch) -> None:
    """A task committed before worker recovery is started exactly once by reconciliation."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="durable start"))
    client = _TemporalClient()

    dispatcher = TemporalCommandDispatcher(client=client, session_factory=session_factory)
    asyncio.run(dispatcher.dispatch_pending())
    asyncio.run(dispatcher.dispatch_pending())

    assert client.started == [snapshot.task_id]
    with session_scope(session_factory) as session:
        assert session.query(TemporalCommand).one().delivered_at is not None
