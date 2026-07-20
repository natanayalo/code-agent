"""Dedicated integration coverage for durable Postgres-to-Temporal handoff."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

from sqlalchemy.pool import StaticPool

from db.base import Base, utc_now
from db.models import Task, TemporalCommand, TemporalTaskState, WorkerRun
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

    async def start_workflow(self, _name, task_id, **_kwargs) -> None:
        await super().start_workflow(_name, task_id, **_kwargs)

    def get_workflow_handle(self, _workflow_id: str) -> _WorkflowHandle:
        return self.handle


class _BlockingStartClient(_CommandClient):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()
        self.allow_start_result = asyncio.Event()

    async def start_workflow(self, _name, task_id, **_kwargs) -> None:
        self.started.append(task_id)
        self.start_entered.set()
        await self.allow_start_result.wait()


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
    """Interaction signals and cancellation follow the durable start command."""
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

    assert client.started == [snapshot.task_id]
    assert client.handle.signals == []
    assert client.handle.cancelled is False

    asyncio.run(dispatcher.dispatch_pending())

    assert client.handle.signals == [
        (
            "handle_approval",
            {"command_key": f"{snapshot.task_id}:signal", "value": True},
        )
    ]
    assert client.handle.cancelled is False

    asyncio.run(dispatcher.dispatch_pending())
    assert client.handle.cancelled is True
    with session_scope(session_factory) as session:
        assert all(command.delivered_at is not None for command in session.query(TemporalCommand))


def test_cancel_supersedes_an_undelivered_temporal_start(monkeypatch) -> None:
    """Cancellation before dispatch resolves both commands without creating a workflow."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="cancel before temporal start"))

    cancelled = service.cancel_task(task_id=snapshot.task_id)
    assert cancelled is not None
    assert cancelled.status == "failed"

    client = _CommandClient()
    asyncio.run(
        TemporalCommandDispatcher(client=client, session_factory=session_factory).dispatch_pending()
    )

    assert client.started == []
    assert client.handle.signals == []
    assert client.handle.cancelled is False
    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        commands = session.query(TemporalCommand).filter_by(task_id=snapshot.task_id).all()
        assert task is not None
        assert task.status.value == "failed"
        assert {command.command_type for command in commands} == {"start", "cancel"}
        assert all(command.superseded_at is not None for command in commands)
        assert all(command.delivered_at is None for command in commands)
        assert session.query(TemporalTaskState).filter_by(task_id=snapshot.task_id).count() == 0
        assert session.query(WorkerRun).filter_by(task_id=snapshot.task_id).count() == 0


def test_cancellation_keeps_a_start_held_by_another_dispatcher_deliverable(monkeypatch) -> None:
    """Cancellation remains queued when another dispatcher already owns the start."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="claimed start cancellation"))
    with session_scope(session_factory) as session:
        start = TemporalCommandRepository(session).claim_pending(limit=1, lease_seconds=30)[0]
        assert start.command_type == "start"
        assert start.claim_token is not None
        start_id = start.id
        claim_token = start.claim_token

    cancelled = service.cancel_task(task_id=snapshot.task_id)
    assert cancelled is not None

    client = _CommandClient()
    dispatcher = TemporalCommandDispatcher(client=client, session_factory=session_factory)
    asyncio.run(dispatcher._dispatch_one(start_id, claim_token))
    asyncio.run(dispatcher.dispatch_pending())

    assert client.started == [snapshot.task_id]
    assert client.handle.cancelled is True
    with session_scope(session_factory) as session:
        commands = {
            command.command_type: command
            for command in session.query(TemporalCommand).filter_by(task_id=snapshot.task_id)
        }
        assert commands["start"].delivered_at is not None
        assert commands["start"].superseded_at is None
        assert commands["cancel"].delivered_at is not None
        assert commands["cancel"].superseded_at is None


def test_cancellation_during_start_rpc_preserves_follow_up_cancel(monkeypatch) -> None:
    """An in-flight start is acknowledged before its pending cancellation is delivered."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="cancel during temporal start"))
    client = _BlockingStartClient()
    dispatcher = TemporalCommandDispatcher(client=client, session_factory=session_factory)

    async def exercise_race() -> None:
        dispatching = asyncio.create_task(dispatcher.dispatch_pending())
        await asyncio.wait_for(client.start_entered.wait(), timeout=1)
        cancelled = service.cancel_task(task_id=snapshot.task_id)
        assert cancelled is not None
        client.allow_start_result.set()
        await dispatching
        await dispatcher.dispatch_pending()

    asyncio.run(exercise_race())

    assert client.started == [snapshot.task_id]
    assert client.handle.cancelled is True
    with session_scope(session_factory) as session:
        task = session.get(Task, snapshot.task_id)
        commands = {
            command.command_type: command
            for command in session.query(TemporalCommand).filter_by(task_id=snapshot.task_id)
        }
        assert task is not None
        assert task.status.value == "failed"
        assert commands["start"].delivered_at is not None
        assert commands["start"].superseded_at is None
        assert commands["cancel"].delivered_at is not None
        assert commands["cancel"].superseded_at is None
        assert session.query(TemporalTaskState).filter_by(task_id=snapshot.task_id).count() == 0
        assert session.query(WorkerRun).filter_by(task_id=snapshot.task_id).count() == 0


def test_concurrent_enqueues_receive_distinct_task_local_sequences(monkeypatch, tmp_path) -> None:
    """Concurrent operator commands cannot collide on a task-local sequence number."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        f"sqlite+pysqlite:///{tmp_path / 'temporal-command-sequences.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="concurrent command sequences"))

    barrier = Barrier(2)

    def enqueue(command_key: str) -> None:
        barrier.wait()
        with session_scope(session_factory) as session:
            TemporalCommandRepository(session).enqueue(
                task_id=snapshot.task_id,
                command_type="signal",
                command_key=command_key,
                payload={"signal_name": "handle_approval", "signal_arg": True},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(enqueue, ["concurrent-signal-1", "concurrent-signal-2"]))

    with session_scope(session_factory) as session:
        commands = (
            session.query(TemporalCommand)
            .filter_by(task_id=snapshot.task_id)
            .order_by(TemporalCommand.sequence_number)
            .all()
        )
        assert [command.sequence_number for command in commands] == [1, 2, 3]


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
    """A second dispatcher cannot overtake an earlier command held by the first."""
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
        assert len(first) == 1
        assert first[0].command_type == "start"

    client = _CommandClient()
    asyncio.run(
        TemporalCommandDispatcher(client=client, session_factory=session_factory).dispatch_pending()
    )
    assert client.handle.signals == []
    assert client.started == []

    with session_scope(session_factory) as session:
        start = (
            session.query(TemporalCommand)
            .filter_by(
                task_id=snapshot.task_id,
                command_type="start",
            )
            .one()
        )
        assert start.claim_token is not None
        start.claim_expires_at = utc_now()

    asyncio.run(
        TemporalCommandDispatcher(client=client, session_factory=session_factory).dispatch_pending()
    )
    assert client.started == [snapshot.task_id]
    assert client.handle.signals == []

    asyncio.run(
        TemporalCommandDispatcher(client=client, session_factory=session_factory).dispatch_pending()
    )
    assert len(client.handle.signals) == 1


def test_workflow_not_found_signal_is_retried_after_start(monkeypatch) -> None:
    """A transient absent workflow cannot permanently discard an interaction signal."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="retry signal"))
    with session_scope(session_factory) as session:
        commands = TemporalCommandRepository(session)
        start = session.query(TemporalCommand).filter_by(task_id=snapshot.task_id).one()
        start.delivered_at = utc_now()
        commands.enqueue(
            task_id=snapshot.task_id,
            command_type="signal",
            command_key="retry-not-found-signal",
            payload={"signal_name": "handle_approval", "signal_arg": True},
        )
        signal = commands.claim_pending(limit=1, lease_seconds=30)[0]

    dispatcher = TemporalCommandDispatcher(client=_CommandClient(), session_factory=session_factory)
    dispatcher._record_failure(signal.id, signal.claim_token, RuntimeError("workflow not found"))

    with session_scope(session_factory) as session:
        signal = session.get(TemporalCommand, signal.id)
        assert signal is not None
        assert signal.dead_lettered_at is None
        assert signal.attempts == 1
        signal.next_attempt_at = utc_now()

    client = _CommandClient()
    asyncio.run(
        TemporalCommandDispatcher(client=client, session_factory=session_factory).dispatch_pending()
    )
    assert client.handle.signals == [
        ("handle_approval", {"command_key": "retry-not-found-signal", "value": True})
    ]


def test_start_and_signal_stay_ordered_across_batch_boundaries(monkeypatch) -> None:
    """A signal at batch N+1 cannot pass a start at the end of batch N."""
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=session_factory, worker=_Worker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="batch order"))
    with session_scope(session_factory) as session:
        TemporalCommandRepository(session).enqueue(
            task_id=snapshot.task_id,
            command_type="signal",
            command_key="batch-boundary-signal",
            payload={"signal_name": "handle_approval", "signal_arg": True},
        )

    client = _CommandClient()
    dispatcher = TemporalCommandDispatcher(
        client=client,
        session_factory=session_factory,
        batch_size=1,
    )
    asyncio.run(dispatcher.dispatch_pending())
    assert client.started == [snapshot.task_id]
    assert client.handle.signals == []

    asyncio.run(dispatcher.dispatch_pending())
    assert client.handle.signals == [
        ("handle_approval", {"command_key": "batch-boundary-signal", "value": True})
    ]


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
