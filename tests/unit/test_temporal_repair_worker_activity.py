"""Idempotency coverage for Temporal repair worker Activities."""

from types import SimpleNamespace

import pytest

from orchestrator.state import OrchestratorState
from orchestrator.temporal.activities import TaskExecutionActivities


@pytest.mark.anyio
async def test_persisted_repair_worker_result_is_not_executed_twice() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-id", "task_text": "Repair this task"},
            "result": {"status": "success", "summary": "repair applied"},
            "completion_loop": {
                "phase": "verification_pending",
                "repair_pass": 1,
                "repair_source": "verifier",
            },
        }
    )
    activities = _activities_for_state(state)

    result = await TaskExecutionActivities.run_worker.__wrapped__(activities, "task-id")

    assert result == {"requires_permission_escalation": False}


@pytest.mark.anyio
async def test_persisted_repair_permission_request_is_reconstructed() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-id", "task_text": "Repair this task"},
            "result": {
                "status": "failure",
                "failure_kind": "permission_denied",
                "summary": "workspace write is required",
                "next_action_hint": "request_higher_permission",
            },
            "completion_loop": {
                "phase": "repair_requested",
                "repair_pass": 1,
                "repair_source": "independent_review",
            },
        }
    )
    activities = _activities_for_state(state)

    result = await TaskExecutionActivities.run_worker.__wrapped__(activities, "task-id")

    assert result == {"requires_permission_escalation": True}


def _activities_for_state(state: OrchestratorState) -> TaskExecutionActivities:
    activities = object.__new__(TaskExecutionActivities)
    activities.service = SimpleNamespace(
        _run_blocking=lambda func, *args, **kwargs: _run_blocking(func, *args, **kwargs)
    )
    activities._get_current_state = lambda _task_id: state
    activities._has_event = lambda *_args: True
    return activities


async def _run_blocking(func, *args, **kwargs):
    return func(*args, **kwargs)
