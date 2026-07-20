"""Focused regression tests for task-control execution branches."""

from __future__ import annotations

import builtins
import logging

from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import (
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    TaskStatus,
    TimelineEventType,
)
from db.models import HumanInteraction, TemporalCommand
from orchestrator import execution as execution_module
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


def test_record_interaction_response_clarification_requeues_without_approval_side_effects(
    monkeypatch,
) -> None:
    """Clarification answers should resume the task without mutating approval state."""
    service, session_factory = _make_task_service()
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="debug this"))

    clarification = next(
        interaction
        for interaction in snapshot.pending_interactions
        if interaction.interaction_type == "clarification"
    )
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
        signals = session.query(TemporalCommand).filter_by(command_type="signal").all()
        assert [(signal.task_id, signal.payload) for signal in signals] == [
            (
                snapshot.task_id,
                {"signal_name": "handle_clarification", "signal_arg": None},
            )
        ]


def test_record_interaction_response_rejects_normal_permission(monkeypatch) -> None:
    """Generic permission rejection must project failure before signaling Temporal."""
    service, session_factory = _make_task_service()
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
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

    refreshed = service.record_interaction_response(
        task_snapshot.task_id,
        permission_interaction.id,
        execution_module.InteractionResponse(response_data={"approved": False}),
    )

    assert refreshed is not None
    assert refreshed.status == TaskStatus.FAILED.value
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED
        assert task.next_attempt_at is None
        assert task.constraints["approval"]["status"] == "rejected"
        assert task.last_error == "Manual approval rejected via interaction response."
        timeline = TaskTimelineRepository(session).list_by_task(task_snapshot.task_id)
        assert timeline[-1].event_type is TimelineEventType.APPROVAL_REJECTED
        signal = session.query(TemporalCommand).filter_by(command_type="signal").one()
        assert signal.payload == {"signal_name": "handle_approval", "signal_arg": False}


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
    monkeypatch.setenv("CODE_AGENT_EXECUTION_RUNTIME", "temporal")
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="debug this"))
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

    refreshed = service.record_interaction_response(
        snapshot.task_id,
        interaction_id,
        execution_module.InteractionResponse(response_data={"approved": False}),
    )

    assert refreshed is not None
    service.record_interaction_response(
        snapshot.task_id,
        interaction_id,
        execution_module.InteractionResponse(response_data={"approved": False}),
    )
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        signals = session.query(TemporalCommand).filter_by(command_type="signal").all()
        assert len(signals) == 1
        assert signals[0].payload == {
            "signal_name": "handle_permission_escalation",
            "signal_arg": False,
        }


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
