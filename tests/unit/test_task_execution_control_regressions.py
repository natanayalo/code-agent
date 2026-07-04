"""Focused regression tests for task-control execution branches."""

from __future__ import annotations

import builtins
import logging

from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import HumanInteractionStatus, HumanInteractionType, TaskStatus
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


def test_record_interaction_response_clarification_requeues_without_approval_side_effects() -> None:
    """Clarification answers should resume the task without mutating approval state."""
    service, session_factory = _make_task_service()
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
