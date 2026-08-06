"""Unit tests for orchestrator/temporal/activities.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db.enums import TaskStatus
from orchestrator.state import NodeOutcome, OrchestratorState
from orchestrator.temporal.activities import (
    TaskExecutionActivities,
    _blocked_permission_outcome,
    _finalize_worker_activity_state,
    _permission_escalation_retry_is_complete,
    _retain_cancelled_workspace_artifact,
    _source_file_changes,
    _worker_state_for_execution,
)
from sandbox.scratch import scratch_namespace_component
from workers import WorkerResult


def test_permission_escalation_retry_is_complete():
    with pytest.raises(RuntimeError, match="unavailable for permission escalation"):
        _permission_escalation_retry_is_complete("t1", None, None, True)

    task = MagicMock()
    task.status = TaskStatus.IN_PROGRESS

    assert _permission_escalation_retry_is_complete("t1", task, MagicMock(), True) is False

    with pytest.raises(RuntimeError, match="unavailable for permission escalation"):
        _permission_escalation_retry_is_complete("t1", task, None, True)

    task.status = TaskStatus.COMPLETED
    assert _permission_escalation_retry_is_complete("t1", task, None, True) is True


def test_blocked_permission_outcome():
    state = OrchestratorState(
        task={"task_text": "text", "repo_url": "url"},
        node_outcomes=[
            NodeOutcome(
                node_id="n1",
                status="blocked",
                attempts=1,
                result=WorkerResult(status="failure", next_action_hint="request_higher_permission"),
            )
        ],
    )
    outcome = _blocked_permission_outcome(state)
    assert outcome is not None and outcome.node_id == "n1"


def test_source_file_changes():
    logical_key = "act-1"
    ns = scratch_namespace_component(logical_key)
    files = [
        "src/main.py",
        f".code-agent/node-runs/{ns}/test.log",
        "README.md",
    ]
    cleaned = _source_file_changes(files, logical_key)
    assert "src/main.py" in cleaned
    assert "README.md" in cleaned
    assert f".code-agent/node-runs/{ns}/test.log" not in cleaned


def test_worker_state_for_execution_and_finalize():
    state = OrchestratorState(
        task={"task_text": "text", "repo_url": "url"},
    )
    state_dict = _worker_state_for_execution(state, repair_execution=True)
    assert state_dict["verification"] is None
    assert state_dict["review"] is None

    final_state, req_perm = _finalize_worker_activity_state(state_dict, repair_execution=True)
    assert req_perm is False
    assert final_state.completion_loop.phase == "verification_pending"


def test_retain_cancelled_workspace_artifact(tmp_path):
    ws_dir = tmp_path / "ws1"
    ws_dir.mkdir()

    state = OrchestratorState(
        task={"task_text": "text", "repo_url": "url"},
        dispatch={
            "workspace_id": "ws1",
            "runtime_manifest": {
                "sandbox": {"workspace_root": str(tmp_path)},
                "worker": {"workspace_id": "ws1"},
            },
        },
        result=WorkerResult(status="failure", summary="cancelled"),
    )
    retained = _retain_cancelled_workspace_artifact(state)
    assert any(art.artifact_type == "workspace" for art in retained.result.artifacts)


def test_capacity_and_activity_helpers():
    svc = MagicMock()
    svc.worker = MagicMock()
    svc.worker_profiles = {}
    svc.enable_worker_profiles = False
    svc.session_factory = MagicMock()
    sess = MagicMock()
    svc.session_factory.return_value.__enter__.return_value = sess

    activities = TaskExecutionActivities(svc)

    with patch(
        "orchestrator.temporal.activities.ExecutionCapacityPermitRepository"
    ) as mock_repo_cls:
        mock_repo_cls.return_value.claim.return_value = True
        mock_repo_cls.return_value.heartbeat.return_value = True
        assert activities._claim_execution_capacity("q", "o", "t") is True
        assert activities._heartbeat_execution_capacity("o", "t") is True
        activities._release_execution_capacity("o", "t")

    with patch("orchestrator.temporal.activities.TaskRepository") as mock_task_repo_cls:
        task = MagicMock()
        task.trace_context = {"traceparent": "00-1234-5678-01"}
        mock_task_repo_cls.return_value.get.return_value = task
        ctx = activities._load_task_trace_context("t1")
        assert ctx.get("traceparent") == "00-1234-5678-01"


@pytest.mark.asyncio
async def test_notify_progress():
    svc = MagicMock()
    svc.worker = MagicMock()
    svc.worker_profiles = {}
    svc.enable_worker_profiles = False
    svc.progress_notifier = AsyncMock()

    activities = TaskExecutionActivities(svc)

    sub = MagicMock()
    sub.task_text = "txt"
    pers = MagicMock()
    pers.session_id = "s1"
    pers.channel = "telegram"
    pers.external_thread_id = "thread-1"

    svc._run_blocking = AsyncMock(return_value=(sub, pers))
    await activities._notify_progress("t1", phase="execution_started", summary="Started")
    svc.progress_notifier.notify.assert_called_once()
