"""Unit tests for orchestrator/execution_interaction_service.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from db.enums import HumanInteractionStatus
from orchestrator.execution_interaction_service import (
    _capture_interaction_resolution_observation,
    _enqueue_approval_signal,
    _enqueue_temporal_signal,
    _persist_resolved_interaction,
    _validate_approval_state,
    cancel_task,
    list_pending_interactions,
    record_interaction_response,
)
from orchestrator.execution_types import InteractionResponse


def test_capture_interaction_resolution_observation():
    session = MagicMock()
    task = MagicMock()
    interaction = MagicMock()
    _capture_interaction_resolution_observation(session=session, task=task, interaction=interaction)


def test_enqueue_temporal_and_approval_signals():
    session = MagicMock()
    with patch(
        "orchestrator.execution_interaction_service.TemporalCommandRepository"
    ) as mock_repo_cls:
        _enqueue_temporal_signal(session, "t1", "sig", True, "k1")
        mock_repo_cls.return_value.enqueue.assert_called_once()

    with patch(
        "orchestrator.execution_interaction_service.TemporalCommandRepository"
    ) as mock_repo_cls:
        _enqueue_approval_signal(session, "t1", True)
        mock_repo_cls.return_value.enqueue.assert_called_once()


def test_persist_resolved_interaction():
    session = MagicMock()
    task = MagicMock()
    task.id = "t1"
    task.constraints = {}
    task.attempt_count = 1
    interaction = MagicMock()
    interaction.id = "i1"
    interaction.data = {"source": "worker_permission_escalation"}
    interaction.interaction_type = "permission"
    interaction.summary = "summary"
    response = InteractionResponse(response_data={"approved": True, "comment": "ok"})
    timeline_repo = MagicMock()

    _persist_resolved_interaction(
        session=session,
        task=task,
        interaction=interaction,
        response=response,
        timeline_repo=timeline_repo,
    )
    timeline_repo.create_next_for_attempt.assert_called_once()


def test_cancel_task():
    svc = MagicMock()
    svc.session_factory = MagicMock()
    sess = MagicMock()
    svc.session_factory.return_value.__enter__.return_value = sess

    task = MagicMock()
    task.id = "t1"
    task.session_id = "s1"
    task.status = "cancelled"

    with (
        patch("orchestrator.execution_interaction_service.TaskRepository") as mock_task_repo_cls,
        patch("orchestrator.execution_interaction_service.WorkerRunRepository"),
        patch("orchestrator.execution_interaction_service.TaskTimelineRepository"),
        patch("orchestrator.execution_interaction_service.TemporalCommandRepository"),
        patch("orchestrator.execution_interaction_service.TemporalTaskStateRepository"),
    ):
        mock_task_repo_cls.return_value.cancel.return_value = (task, True)
        mock_task_repo_cls.return_value.get.return_value = task
        res = cancel_task(svc, task_id="t1")
        assert res is not None


def test_validate_approval_state():
    svc = MagicMock()
    # None
    r1 = _validate_approval_state(svc, "t1", None, True)
    assert r1 is not None and r1.status == "not_waiting"

    # Already applied
    r2 = _validate_approval_state(svc, "t1", {"status": "approved"}, True)
    assert r2 is not None and r2.status == "already_applied"

    # Conflict
    r3 = _validate_approval_state(svc, "t1", {"status": "approved"}, False)
    assert r3 is not None and r3.status == "conflict"

    # Pending
    r4 = _validate_approval_state(svc, "t1", {"status": "pending"}, True)
    assert r4 is None


def test_list_pending_interactions_and_record():
    svc = MagicMock()
    svc.session_factory = MagicMock()
    sess = MagicMock()
    svc.session_factory.return_value.__enter__.return_value = sess

    with patch(
        "orchestrator.execution_interaction_service.HumanInteractionRepository"
    ) as mock_repo_cls:
        mock_repo_cls.return_value.list_pending_with_task_context.return_value = []
        assert list_pending_interactions(svc) == []

    task = MagicMock()
    task.id = "t1"
    task.constraints = {}
    task.orchestration_runtime = "in_memory"

    interaction = MagicMock()
    interaction.id = "i1"
    interaction.interaction_type = "permission"
    interaction.summary = "summary"
    interaction.data = {"source": "worker"}
    interaction.status = HumanInteractionStatus.RESOLVED

    with (
        patch("orchestrator.execution_interaction_service.TaskRepository") as mock_t_cls,
        patch(
            "orchestrator.execution_interaction_service.HumanInteractionRepository"
        ) as mock_hi_cls,
        patch("orchestrator.execution_interaction_service.TaskTimelineRepository"),
    ):
        mock_t_cls.return_value.get.return_value = task
        mock_hi_cls.return_value.record_response.return_value = (interaction, True)
        svc.get_task.return_value = MagicMock()
        res = record_interaction_response(
            svc, "t1", "i1", InteractionResponse(response_data={"approved": True})
        )
        assert res is not None
