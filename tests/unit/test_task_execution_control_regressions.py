"""Focused regression tests for task-control execution branches."""

from __future__ import annotations

import asyncio
import builtins
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import (
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    TimelineEventType,
)
from db.models import HumanInteraction
from orchestrator import execution as execution_module
from orchestrator import execution_interaction_service as interaction_module
from repositories import (
    HumanInteractionRepository,
    TaskRepository,
    TaskTimelineRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


class _StaticWorker(Worker):
    """Minimal worker double for initializing the execution service."""

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


def test_temporal_client_cache_is_scoped_to_event_loop(monkeypatch) -> None:
    """Sync fallbacks must not reuse a client bound to a closed event loop."""
    service, _ = _make_task_service()
    clients: list[object] = []

    async def connect(_address: str) -> object:
        client = object()
        clients.append(client)
        return client

    from temporalio.client import Client

    monkeypatch.setattr(Client, "connect", connect)

    first_client = asyncio.run(service._get_temporal_client())
    second_client = asyncio.run(service._get_temporal_client())
    third_client = asyncio.run(service._get_temporal_client())

    assert first_client is clients[0]
    assert second_client is clients[1]
    assert first_client is not second_client
    assert third_client is clients[2]
    assert len(service._temporal_clients) == 1
    assert len(service._temporal_locks) == 1


def test_temporal_client_cache_supports_concurrent_event_loops(monkeypatch) -> None:
    """Concurrent sync fallbacks must keep each loop's client isolated."""
    service, _ = _make_task_service()
    clients: list[object] = []
    both_connecting = Barrier(2)

    async def connect(_address: str) -> object:
        client = object()
        clients.append(client)
        both_connecting.wait()
        return client

    from temporalio.client import Client

    monkeypatch.setattr(Client, "connect", connect)

    def get_client(_index: int) -> object:
        return asyncio.run(service._get_temporal_client())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(get_client, range(2)))

    assert len(set(results)) == 2
    assert len(clients) == 2
    assert len(service._temporal_clients) == 2
    assert len(service._temporal_locks) == 2


def test_start_temporal_workflow_sync_dispatches_background_thread(monkeypatch) -> None:
    """Sync submission must not block while Temporal retries connection failures."""
    service, _ = _make_task_service()
    started: list[tuple[object, bool]] = []

    class FakeThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            started.append((self.target, self.daemon))

    monkeypatch.setattr(execution_module.threading, "Thread", FakeThread)

    service.start_temporal_workflow_sync("task-id")

    assert len(started) == 1
    assert started[0][1] is True


def test_record_interaction_response_clarification_requeues_without_approval_side_effects(
    monkeypatch,
) -> None:
    """Clarification answers should resume the task without mutating approval state."""
    service, session_factory = _make_task_service()
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="debug this"))

    clarification = next(
        interaction
        for interaction in snapshot.pending_interactions
        if interaction.interaction_type == "clarification"
    )
    temporal_signals: list[tuple[str, str, object]] = []
    active_transactions = 0
    original_session_scope = interaction_module.session_scope

    @contextmanager
    def tracking_session_scope(factory):
        nonlocal active_transactions
        active_transactions += 1
        try:
            with original_session_scope(factory) as session:
                yield session
        finally:
            active_transactions -= 1

    async def signal_temporal_workflow(task_id: str, signal_name: str, arg: object) -> None:
        assert active_transactions == 0
        temporal_signals.append((task_id, signal_name, arg))

    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    monkeypatch.setattr(interaction_module, "session_scope", tracking_session_scope)
    monkeypatch.setattr(service, "signal_temporal_workflow", signal_temporal_workflow)

    refreshed = service.record_interaction_response(
        snapshot.task_id,
        clarification.interaction_id,
        execution_module.InteractionResponse(
            response_data={"repo": "code-agent", "symptom": "failing retry path"}
        ),
    )

    assert refreshed is not None
    assert refreshed.status == TaskStatus.PENDING.value
    assert refreshed.pending_interaction_count == 0
    assert refreshed.pending_interactions == []
    assert any(event.event_type == "task_spec_and_route_generated" for event in refreshed.timeline)
    assert temporal_signals == [(snapshot.task_id, "handle_clarification", None)]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        interactions = HumanInteractionRepository(session).list_by_task(task_id=snapshot.task_id)
        timeline = TaskTimelineRepository(session).list_by_task(snapshot.task_id)

        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert "approval" not in (task.constraints or {})
        assert "requires_approval" not in (task.constraints or {})
        assert len(interactions) == 1
        assert interactions[0].interaction_type is HumanInteractionType.CLARIFICATION
        assert interactions[0].status is HumanInteractionStatus.RESOLVED
        assert interactions[0].response_data == {
            "repo": "code-agent",
            "symptom": "failing retry path",
        }
        assert timeline[-1].event_type.value == "task_spec_and_route_generated"


def test_record_interaction_response_rejects_normal_permission(monkeypatch) -> None:
    """Generic permission rejection must project failure before signaling Temporal."""
    service, session_factory = _make_task_service()
    task_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="Reject elevated permission",
            constraints={"requires_approval": True},
        )
    )

    with session_scope(session_factory) as session:
        HumanInteractionRepository(session).sync_task_spec_flags(
            task_id=task_snapshot.task_id,
            task_spec={
                "requires_permission": True,
                "permission_reason": "Need workspace write access.",
                "risk_level": "high",
            },
        )
        permission_interaction = next(
            row
            for row in HumanInteractionRepository(session).list_by_task(
                task_id=task_snapshot.task_id
            )
            if row.interaction_type is HumanInteractionType.PERMISSION
        )

    temporal_signals: list[tuple[str, str, object]] = []

    async def signal_temporal_workflow(task_id: str, signal_name: str, arg: object) -> None:
        temporal_signals.append((task_id, signal_name, arg))

    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    monkeypatch.setattr(service, "signal_temporal_workflow", signal_temporal_workflow)
    refreshed = service.record_interaction_response(
        task_snapshot.task_id,
        permission_interaction.id,
        execution_module.InteractionResponse(response_data={"approved": False}),
    )

    assert refreshed is not None
    assert refreshed.status == TaskStatus.FAILED.value
    assert temporal_signals == [(task_snapshot.task_id, "handle_approval", False)]
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED
        assert task.next_attempt_at is None
        assert task.constraints["approval"]["status"] == "rejected"
        assert task.last_error == "Manual approval rejected via interaction response."
        timeline = TaskTimelineRepository(session).list_by_task(task_snapshot.task_id)
        assert timeline[-1].event_type is TimelineEventType.APPROVAL_REJECTED


def test_record_interaction_response_ignores_missing_observation_dependency(
    monkeypatch,
) -> None:
    """Missing observation helpers should not block interaction resolution."""
    service, session_factory = _make_task_service()
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="debug this"))

    clarification = next(
        interaction
        for interaction in snapshot.pending_interactions
        if interaction.interaction_type == "clarification"
    )
    original_import = builtins.__import__

    def _raise_for_observation(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "memory.observation":
            raise ImportError("memory observation unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _raise_for_observation)

    refreshed = service.record_interaction_response(
        snapshot.task_id,
        clarification.interaction_id,
        execution_module.InteractionResponse(response_data={"answer": "continue"}),
    )

    assert refreshed is not None
    assert refreshed.status == TaskStatus.PENDING.value


def test_permission_escalation_response_signals_dedicated_temporal_handler(monkeypatch) -> None:
    """Worker escalation responses must not be confused with task approval."""
    service, session_factory = _make_task_service()
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="debug this"))
    temporal_signals: list[tuple[str, str, object]] = []

    async def signal_temporal_workflow(task_id: str, signal_name: str, arg: object) -> None:
        temporal_signals.append((task_id, signal_name, arg))

    with session_scope(session_factory) as session:
        interaction = HumanInteraction(
            task_id=snapshot.task_id,
            interaction_type=HumanInteractionType.PERMISSION,
            status=HumanInteractionStatus.PENDING,
            hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
            summary="Worker needs workspace_write.",
            data={"source": "worker_permission_escalation"},
        )
        session.add(interaction)
        session.flush()
        interaction_id = interaction.id

    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    monkeypatch.setattr(service, "signal_temporal_workflow", signal_temporal_workflow)
    refreshed = service.record_interaction_response(
        snapshot.task_id,
        interaction_id,
        execution_module.InteractionResponse(response_data={"approved": False}),
    )

    assert refreshed is not None
    assert temporal_signals == [(snapshot.task_id, "handle_permission_escalation", False)]

    service.record_interaction_response(
        snapshot.task_id,
        interaction_id,
        execution_module.InteractionResponse(response_data={"approved": False}),
    )
    assert temporal_signals == [(snapshot.task_id, "handle_permission_escalation", False)]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING


def test_record_interaction_response_logs_observation_capture_failures(
    monkeypatch,
    caplog,
) -> None:
    """Unexpected observation capture failures should warn without blocking."""
    service, session_factory = _make_task_service()
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="debug this"))

    clarification = next(
        interaction
        for interaction in snapshot.pending_interactions
        if interaction.interaction_type == "clarification"
    )

    from memory.observation import ObservationCaptureService

    def _raise_capture(*, session, task, interaction):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        ObservationCaptureService,
        "capture_interaction_resolution",
        _raise_capture,
    )

    with caplog.at_level(logging.WARNING, logger="orchestrator.execution_interaction_service"):
        refreshed = service.record_interaction_response(
            snapshot.task_id,
            clarification.interaction_id,
            execution_module.InteractionResponse(response_data={"answer": "continue"}),
        )

    assert refreshed is not None
    assert refreshed.status == TaskStatus.PENDING.value
    assert "Failed to capture interaction resolution observation; continuing." in caplog.text


def test_replay_task_normalizes_malformed_provenance_chain(caplog) -> None:
    """Replay should reset malformed provenance state and prepend the immediate source task."""
    service, session_factory = _make_task_service()
    source_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="Create a note and report the result",
            constraints={
                "assumptions": ["first pass"],
                "replayed_from": {"legacy": "bad-shape"},
            },
        )
    )

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(source_snapshot.task_id)
        assert task is not None
        task.status = TaskStatus.COMPLETED
        session.flush()

    with caplog.at_level(logging.WARNING, logger=execution_module.logger.name):
        replayed = service.replay_task(
            source_task_id=source_snapshot.task_id,
            replay_request=execution_module.TaskReplayRequest(
                constraints={"assumptions": ["second pass"]}
            ),
        )

    assert replayed.status == "created"
    assert replayed.task_snapshot is not None
    assert "Unexpected replayed_from type" in caplog.text

    with session_scope(session_factory) as session:
        replay_task = TaskRepository(session).get(replayed.task_snapshot.task_id)

        assert replay_task is not None
        assert replay_task.constraints["replayed_from"] == [source_snapshot.task_id]
        assert replay_task.constraints["assumptions"] == ["second pass"]
