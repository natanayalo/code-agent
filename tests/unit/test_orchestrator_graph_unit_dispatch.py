# ruff: noqa: F403, F405
"""Dispatch and control-flow orchestrator graph unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403


def test_dispatch_job_preserves_attempt_count():
    """dispatch_job must preserve attempt_count (it is managed externally)."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {"chosen_worker": "codex", "route_reason": "cheap_mechanical_change"},
            "attempt_count": 0,
        }
    )
    result = dispatch_job(state)
    assert result["current_step"] == "dispatch_job"
    assert result["repair_handoff_requested"] is False


def test_dispatch_job_preserves_attempt_count_on_retry():
    """attempt_count remains constant throughout a single graph invocation."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": "antigravity",
                "route_reason": "verifier_failed_previous_run",
            },
            "attempt_count": 1,
        }
    )
    result = dispatch_job(state)
    assert result["current_step"] == "dispatch_job"
    assert result["repair_handoff_requested"] is False


def test_dispatch_job_includes_route_profile_metadata() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": "codex",
                "chosen_profile": "codex-native-executor",
                "runtime_mode": "native_agent",
            },
        }
    )

    result = dispatch_job(state)

    assert result["dispatch"]["worker_type"] == "codex"
    assert result["dispatch"]["worker_profile"] == "codex-native-executor"
    assert result["dispatch"]["runtime_mode"] == "native_agent"


@pytest.mark.anyio
async def test_await_worker_with_timeout_partial_result():
    class SlowWorker(Worker):
        async def run(self, request):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return WorkerResult(
                    status="error",
                    summary="partial state flushed",
                    next_action_hint="inspect_workspace_artifacts",
                    commands_run=[{"command": "echo 1"}],
                )
            return WorkerResult(status="success", summary="done")

    worker = SlowWorker()
    res, hint = await _await_worker_with_timeout(
        worker,
        request=WorkerRequest(session_id="test", task_text="test"),
        worker_type="slow",
        session_id="test",
        timeout_seconds=1,
    )

    assert res.status == "error"
    assert res.summary == "partial state flushed"
    assert res.commands_run[0].command == "echo 1"
    assert hint == "worker timed out but yielded partial state after 1s"


def test_dispatch_job_preserves_workspace_id() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": "codex",
                "route_reason": "cheap_mechanical_change",
            },
            "dispatch": {"workspace_id": "ws_123"},
        }
    )
    res = dispatch_job(state)
    assert res["dispatch"]["workspace_id"] == "ws_123"


def test_dispatch_job_raises_value_error_if_no_worker() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {
                "chosen_worker": None,
                "route_reason": "some_reason",
            },
        }
    )
    with pytest.raises(ValueError, match="choose_worker must set route.chosen_worker"):
        dispatch_job(state)
