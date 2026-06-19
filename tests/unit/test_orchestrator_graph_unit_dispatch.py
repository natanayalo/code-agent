# ruff: noqa: F403, F405
"""Dispatch and control-flow orchestrator graph unit tests."""

from __future__ import annotations

from tests.unit.orchestrator_graph_unit_support import *  # noqa: F403


def test_await_approval_skips_when_not_required() -> None:
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    state.approval.required = False

    res = await_approval(state)

    assert res["current_step"] == "await_approval"


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


def test_route_after_review_result_dispatches_on_repair_handoff():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "repair_handoff_requested": True}
    )

    assert _route_after_review_result(state) == "provision_workspace"


def test_route_after_review_result_delivers_without_repair_handoff():
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})

    assert _route_after_review_result(state) == "deliver_result"


def test_await_permission_escalation_approved():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "next_action_hint": "request_higher_permission",
                "requested_permission": "networked_write",
                "summary": "needs high permission",
            },
        }
    )
    with patch("orchestrator.graph.interrupt", return_value=True):
        res = await_permission_escalation(state)
    assert res["current_step"] == "await_permission_escalation"
    assert res["result"] is None
    assert res["task"]["constraints"]["granted_permission"] == "networked_write"


def test_await_permission_escalation_rejected():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "next_action_hint": "request_higher_permission",
                "requested_permission": "networked_write",
                "summary": "needs high permission",
            },
        }
    )
    with patch("orchestrator.graph.interrupt", return_value=False):
        res = await_permission_escalation(state)
    assert res["current_step"] == "await_permission_escalation"
    assert (
        res["result"]["summary"]
        == "Permission escalation to 'networked_write' was rejected. Run halted."
    )
    assert res["result"]["failure_kind"] == "permission_denied"
    assert res["result"]["next_action_hint"] == "await_manual_follow_up"


def test_await_permission_escalation_missing_permission():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "next_action_hint": "request_higher_permission",
                "summary": "needs high permission",
            },
        }
    )
    res = await_permission_escalation(state)
    assert res["current_step"] == "await_permission_escalation"
    assert res["result"]["status"] == "error"
    assert res["result"]["next_action_hint"] == "inspect_worker_configuration"


def test_await_permission_escalation_invalid_permission():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "next_action_hint": "request_higher_permission",
                "requested_permission": "network_write",
                "summary": "needs high permission",
            },
        }
    )
    res = await_permission_escalation(state)
    assert res["current_step"] == "await_permission_escalation"
    assert res["result"]["status"] == "error"
    assert (
        res["result"]["summary"] == "Worker requested an unknown permission level 'network_write'."
    )
    assert res["result"]["requested_permission"] is None
    assert res["result"]["next_action_hint"] == "inspect_worker_configuration"


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


def test_route_after_await_permission_escalation_routing() -> None:
    from orchestrator.graph import _route_after_await_permission_escalation

    state_no_result = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    assert _route_after_await_permission_escalation(state_no_result) == "provision_workspace"

    state_with_result = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {"status": "failure", "summary": "fail", "failure_kind": "unknown"},
        }
    )
    assert _route_after_await_permission_escalation(state_with_result) == "verify_result"


def test_route_after_init_environment_routing() -> None:
    from orchestrator.graph import _route_after_init_environment

    state_success = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": WorkerResult(status="success", summary="done"),
        }
    )
    assert _route_after_init_environment(state_success) == "dispatch_job"

    state_failure = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": WorkerResult(status="failure", summary="failed"),
        }
    )
    assert _route_after_init_environment(state_failure) == "summarize_result"

    state_error = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": WorkerResult(status="error", summary="error"),
        }
    )
    assert _route_after_init_environment(state_error) == "summarize_result"

    state_interrupt = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": WorkerResult(
                status="failure",
                summary="interrupted",
                next_action_hint="await_manual_follow_up",
            ),
        }
    )
    assert _route_after_init_environment(state_interrupt) == "summarize_result"
