"""Dedicated integration coverage for durable Postgres-to-Temporal handoff."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.pool import StaticPool

from db.base import Base, utc_now
from db.models import TemporalCommand
from orchestrator.execution import TaskExecutionService, TaskSubmission
from orchestrator.temporal.command_dispatcher import TemporalCommandDispatcher
from orchestrator.temporal.workflows import TaskExecutionWorkflow
from repositories import (
    TemporalCommandRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


class _Worker(Worker):
    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary="unused")


class _TemporalClient:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def start_workflow(self, _name, task_id, **_kwargs) -> None:
        self.started.append(task_id)


class _WorkflowHandle:
    def __init__(self) -> None:
        self.signals: list[tuple[str, object]] = []
        self.cancelled = False

    async def signal(self, name: str, value: object) -> None:
        self.signals.append((name, value))

    async def cancel(self) -> None:
        self.cancelled = True


class _CommandClient(_TemporalClient):
    def __init__(self) -> None:
        super().__init__()
        self.handle = _WorkflowHandle()

    def get_workflow_handle(self, _workflow_id: str) -> _WorkflowHandle:
        return self.handle


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


def test_temporal_signal_and_cancel_commands_are_delivered(monkeypatch) -> None:
    """Interaction signals and cancellation use the same durable dispatcher."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="durable commands"))
    with session_scope(session_factory) as session:
        commands = TemporalCommandRepository(session)
        commands.enqueue(
            task_id=snapshot.task_id,
            command_type="signal",
            command_key=f"{snapshot.task_id}:signal",
            payload={"signal_name": "handle_approval", "signal_arg": True},
        )
        commands.enqueue(
            task_id=snapshot.task_id,
            command_type="cancel",
            command_key=f"{snapshot.task_id}:cancel",
            payload={},
        )

    client = _CommandClient()
    dispatcher = TemporalCommandDispatcher(client=client, session_factory=session_factory)
    asyncio.run(dispatcher.dispatch_pending())

    assert client.handle.signals == [
        (
            "handle_approval",
            {"command_key": f"{snapshot.task_id}:signal", "value": True},
        )
    ]
    assert client.handle.cancelled is True
    with session_scope(session_factory) as session:
        assert all(command.delivered_at is not None for command in session.query(TemporalCommand))


def test_temporal_command_delivery_failure_remains_pending_for_retry(monkeypatch) -> None:
    """A transport failure records an attempt without discarding the command."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="retry command"))

    class _FailingClient:
        async def start_workflow(self, *_args, **_kwargs) -> None:
            raise ConnectionError("Temporal unavailable")

    dispatcher = TemporalCommandDispatcher(client=_FailingClient(), session_factory=session_factory)
    asyncio.run(dispatcher.dispatch_pending())

    with session_scope(session_factory) as session:
        command = session.query(TemporalCommand).one()
        assert command.task_id == snapshot.task_id
        assert command.delivered_at is None
        assert command.attempts == 1
        assert command.last_error == "Temporal unavailable"


def test_unknown_temporal_command_type_is_rejected() -> None:
    """Dispatcher rejects unexpected rows instead of sending an ambiguous RPC."""
    dispatcher = TemporalCommandDispatcher(client=_CommandClient(), session_factory=object())
    command = SimpleNamespace(task_id="task-id", command_type="unexpected", payload={})

    try:
        asyncio.run(
            dispatcher._deliver(
                task_id=command.task_id,
                command_type=command.command_type,
                command_key="unexpected-key",
                payload=command.payload,
            )
        )
    except ValueError as exc:
        assert str(exc) == "Unknown Temporal command type: unexpected"
    else:  # pragma: no cover - protects the assertion if behavior changes
        raise AssertionError("Unexpected command type should be rejected.")


def test_two_dispatchers_claim_only_one_logical_signal(monkeypatch) -> None:
    """A second dispatcher cannot deliver a command fenced by the first claim."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="fenced signal"))
    with session_scope(session_factory) as session:
        TemporalCommandRepository(session).enqueue(
            task_id=snapshot.task_id,
            command_type="signal",
            command_key="fenced-signal",
            payload={"signal_name": "handle_approval", "signal_arg": True},
        )
    with session_scope(session_factory) as session:
        first = TemporalCommandRepository(session).claim_pending(limit=10, lease_seconds=30)
        assert len(first) == 2

    client = _CommandClient()
    asyncio.run(
        TemporalCommandDispatcher(client=client, session_factory=session_factory).dispatch_pending()
    )
    assert client.handle.signals == []

    with session_scope(session_factory) as session:
        signal = session.query(TemporalCommand).filter_by(command_key="fenced-signal").one()
        assert signal.claim_token is not None
        signal.claim_expires_at = utc_now()

    asyncio.run(
        TemporalCommandDispatcher(client=client, session_factory=session_factory).dispatch_pending()
    )
    assert len(client.handle.signals) == 1


def test_workflow_signal_envelope_ignores_stale_permission_command() -> None:
    """A duplicate escalation-one signal cannot approve escalation two."""
    workflow = TaskExecutionWorkflow()
    asyncio.run(
        workflow.handle_permission_escalation({"command_key": "escalation-1", "value": True})
    )
    assert workflow.permission_escalation_decision is True

    workflow.permission_escalation_decision = None
    asyncio.run(
        workflow.handle_permission_escalation({"command_key": "escalation-1", "value": True})
    )
    assert workflow.permission_escalation_decision is None
