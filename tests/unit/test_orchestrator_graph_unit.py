"""Unit tests for the orchestrator graph internals."""

import asyncio
from unittest.mock import patch

import pytest

from orchestrator.checkpoints import create_in_memory_checkpointer
from orchestrator.graph import (
    _build_worker_request,
    _classify_task_kind,
    _coerce_approval_decision,
    _compute_route_decision,
    _default_worker_result_provider,
    _ensure_state,
    _is_destructive_task,
    _resolve_orchestrator_timeout_seconds,
    await_approval,
    await_permission_escalation,
    build_choose_worker_node,
    choose_worker,
    summarize_result,
    verify_result,
)
from orchestrator.state import OrchestratorState
from workers import WorkerRequest, WorkerResult


def test_ensure_state_from_dict():
    raw_dict = {"task": {"task_text": "do something"}}
    state = _ensure_state(raw_dict)
    assert isinstance(state, OrchestratorState)
    assert state.task.task_text == "do something"


def test_classify_task_kind():
    assert _classify_task_kind("hello") == "implementation"
    assert _classify_task_kind("refactor code") == "architecture"
    assert _classify_task_kind("investigate logs") == "ambiguous"


def test_is_destructive_task():
    assert _is_destructive_task("test", {"destructive_action": True}) is True


def test_coerce_approval_decision():
    # boolean
    assert _coerce_approval_decision(True) is True
    # dict with boolean
    assert _coerce_approval_decision({"approved": True}) is True
    assert _coerce_approval_decision({"approved": False}) is False
    # dict with string
    assert _coerce_approval_decision({"approved": "y"}) is True
    assert _coerce_approval_decision({"approved": "no"}) is False
    # dict with invalid value
    assert _coerce_approval_decision({"approved": 123}) is False
    # empty or irrelevant dict
    assert _coerce_approval_decision({"other": "field"}) is False
    # direct string
    assert _coerce_approval_decision("yes") is True
    assert _coerce_approval_decision("no") is False


def test_default_worker_result_provider():
    request = WorkerRequest(task_text="demo")
    res = _default_worker_result_provider(request)
    assert res.status == "success"


def test_build_worker_request_from_state():
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "session-1",
                "user_id": "user-1",
                "channel": "telegram",
                "external_thread_id": "thread-1",
            },
            "task": {
                "task_text": "Add worker interface",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "branch": "task/t-040-worker-interface",
                "constraints": {"requires_approval": False},
                "budget": {"max_minutes": 15},
            },
        }
    )
    request = _build_worker_request(state)
    assert request.session_id == "session-1"
    assert request.repo_url == "https://github.com/natanayalo/code-agent"
    assert request.branch == "task/t-040-worker-interface"
    assert request.task_text == "Add worker interface"
    assert request.constraints == {"requires_approval": False}
    assert request.budget == {"max_minutes": 15}


def test_resolve_orchestrator_timeout_seconds_prefers_explicit_override() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {
                    "orchestrator_timeout_seconds": "45",
                    "worker_timeout_seconds": 12,
                    "max_minutes": 9,
                },
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 45


def test_resolve_orchestrator_timeout_seconds_accepts_float_like_strings() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"orchestrator_timeout_seconds": "45.0"},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 45


def test_resolve_orchestrator_timeout_seconds_falls_back_to_worker_budget() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"worker_timeout_seconds": 12},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 42


def test_resolve_orchestrator_timeout_seconds_falls_back_to_max_minutes() -> None:
    state = OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "Run a worker",
                "budget": {"max_minutes": 2},
            }
        }
    )

    assert _resolve_orchestrator_timeout_seconds(state) == 150


def test_choose_worker_override():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "codex"}}
    )
    res = choose_worker(state)
    assert res["route"]["chosen_worker"] == "codex"
    assert res["route"]["route_reason"] == "manual_override"
    assert res["route"]["override_applied"] is True


def test_choose_worker_architecture_default():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
    res = choose_worker(state)
    assert res["route"]["chosen_worker"] == "gemini"
    assert res["route"]["route_reason"] == "high_stakes_refactor"


# ---------------------------------------------------------------------------
# _compute_route_decision unit tests (T-071 / T-072)
# ---------------------------------------------------------------------------

_ALL_WORKERS: frozenset[str] = frozenset({"codex", "gemini"})
_CODEX_ONLY: frozenset[str] = frozenset({"codex"})
_GEMINI_ONLY: frozenset[str] = frozenset({"gemini"})


def test_compute_route_override_available():
    """T-072: manual override is honoured when the worker is available."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "gemini"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "manual_override"
    assert route.override_applied is True


def test_compute_route_override_unavailable_fails_explicitly():
    """T-072: manual override for an unavailable worker returns runtime_unavailable."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "gemini"}}
    )
    route = _compute_route_decision(state, _CODEX_ONLY)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "runtime_unavailable"
    assert route.override_applied is True


def test_compute_route_codex_override_available():
    """T-072: codex manual override is honoured."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "codex"}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "manual_override"


def test_compute_route_budget_prefer_high_quality():
    """T-071: prefer_high_quality budget hint routes to gemini."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "budget": {"prefer_high_quality": True}}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "budget_preference"


def test_compute_route_budget_prefer_low_cost():
    """T-071: prefer_low_cost budget hint routes to codex."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "budget": {"prefer_low_cost": True}}}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "budget_preference"


def test_compute_route_budget_prefer_high_quality_fallback_when_gemini_unavailable():
    """T-071: falls back to codex with preferred_unavailable when gemini isn't configured."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "budget": {"prefer_high_quality": True}}}
    )
    route = _compute_route_decision(state, _CODEX_ONLY)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "preferred_unavailable"


def test_compute_route_task_kind_architecture():
    """T-071: architecture task shape routes to gemini with high_stakes_refactor."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "refactor the module"}, "task_kind": "architecture"}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "high_stakes_refactor"


def test_compute_route_task_kind_ambiguous():
    """T-071: ambiguous task shape routes to gemini with ambiguous_task."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "investigate logs"}, "task_kind": "ambiguous"}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "ambiguous_task"


def test_compute_route_task_kind_implementation():
    """T-071: implementation task shape routes to codex with cheap_mechanical_change."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "add a helper"}, "task_kind": "implementation"}
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "cheap_mechanical_change"


def test_compute_route_task_kind_implementation_fallback_when_codex_unavailable():
    """T-071: falls back to gemini with preferred_unavailable when codex is unavailable."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "add a helper"}, "task_kind": "implementation"}
    )
    route = _compute_route_decision(state, _GEMINI_ONLY)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "preferred_unavailable"


def test_compute_route_verifier_failure_escalation():
    """T-071: failed prior verifier escalates to alternate worker."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "verification": {
                "status": "failed",
                "summary": "tests failed",
                "items": [{"label": "worker_status", "status": "failed"}],
            },
            "result": {
                "status": "failure",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "verifier_failed_previous_run"


def test_compute_route_previous_worker_failed_escalation():
    """T-071: non-success result with no prior verifier escalates via previous_worker_failed."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "failure",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "previous_worker_failed"


def test_compute_route_escalation_fails_explicitly_when_alternate_unavailable():
    """T-071: escalation needed but alternate unavailable → explicit failure, not blind retry."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "task_kind": "implementation",
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "verification": {
                "status": "failed",
                "summary": "tests failed",
                "items": [{"label": "worker_status", "status": "failed"}],
            },
            "result": {
                "status": "failure",
                "commands_run": [],
                "files_changed": [],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    # Only codex is available; gemini escalation not possible → explicit failure, not blind retry.
    route = _compute_route_decision(state, _CODEX_ONLY)
    assert route.chosen_worker == "gemini"  # desired alternate
    assert route.route_reason == "runtime_unavailable"


def test_build_choose_worker_node_binds_available_workers():
    """build_choose_worker_node creates a node that uses the bound available workers."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
    # With only codex available, gemini-preferred architecture task falls back to codex.
    node = build_choose_worker_node(frozenset({"codex"}))
    res = node(state)
    assert res["route"]["chosen_worker"] == "codex"
    assert res["route"]["route_reason"] == "preferred_unavailable"


def test_compute_route_neither_worker_available():
    """_route_by_preference keeps the preferred intent when neither worker is available."""
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
    # Empty available set: neither gemini nor codex is configured.
    route = _compute_route_decision(state, frozenset())
    assert route.chosen_worker == "gemini"
    assert route.route_reason == "runtime_unavailable"


def test_compute_route_escalation_skipped_when_prior_attempt_succeeded():
    """T-071: no escalation when the prior attempt produced a successful result."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "fix the code"},
            "task_kind": "implementation",
            "attempt_count": 1,
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "success",
                "commands_run": [],
                "files_changed": ["x.py"],
                "test_results": [],
                "artifacts": [],
            },
        }
    )
    # Prior attempt succeeded → escalation skipped → falls through to task shape.
    route = _compute_route_decision(state, _ALL_WORKERS)
    assert route.chosen_worker == "codex"
    assert route.route_reason == "cheap_mechanical_change"

    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    state.approval.required = False
    res = await_approval(state)
    assert res["current_step"] == "await_approval"


def test_summarize_result_no_result():
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    res = summarize_result(state)
    assert res["result"]["status"] == "error"


def test_summarize_result_uses_normalized_task_text_for_active_goal():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "  demo  "},
            "normalized_task_text": "demo",
            "result": {
                "status": "success",
                "summary": "done",
                "commands_run": [],
                "files_changed": ["demo.txt"],
                "test_results": [],
                "artifacts": [],
            },
        }
    )

    res = summarize_result(state)

    assert res["session_state_update"]["active_goal"] == "demo"
    assert res["session_state_update"]["files_touched"] == ["demo.txt"]


def test_create_in_memory_checkpointer():
    cp = create_in_memory_checkpointer()
    assert cp is not None


def test_dispatch_job_preserves_attempt_count():
    """dispatch_job must preserve attempt_count (it is managed externally)."""
    from orchestrator.graph import dispatch_job

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {"chosen_worker": "codex", "route_reason": "cheap_mechanical_change"},
            "attempt_count": 0,
        }
    )
    result = dispatch_job(state)
    assert result["current_step"] == "dispatch_job"


def test_dispatch_job_preserves_attempt_count_on_retry():
    """attempt_count remains constant throughout a single graph invocation."""
    from orchestrator.graph import dispatch_job

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "route": {"chosen_worker": "gemini", "route_reason": "verifier_failed_previous_run"},
            "attempt_count": 1,
        }
    )
    result = dispatch_job(state)
    assert result["current_step"] == "dispatch_job"


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
    assert res["result"]["next_action_hint"] == "inspect_worker_configuration"


@pytest.mark.anyio
async def test_await_worker_with_timeout_partial_result():
    from orchestrator.graph import _await_worker_with_timeout
    from workers.base import Worker

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


def test_verify_result_passed():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["file1.py"],
                "test_results": [{"name": "test1", "status": "passed"}],
                "commands_run": [{"command": "pytest", "exit_code": 0}],
            },
        }
    )
    res = verify_result(state)
    assert res["current_step"] == "verify_result"
    assert res["verification"]["status"] == "passed"
    # Status, Tests, Files, Commands
    assert len(res["verification"]["items"]) == 4


def test_verify_result_failed_tests():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": ["file1.py"],
                "test_results": [{"name": "test1", "status": "failed"}],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "failed"
    assert res["verification"]["items"][1]["label"] == "test_results"
    assert res["verification"]["items"][1]["status"] == "failed"


def test_verify_result_warning_no_changes():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "success",
                "files_changed": [],
                "test_results": [{"name": "test1", "status": "passed"}],
            },
        }
    )
    res = verify_result(state)
    assert res["verification"]["status"] == "warning"
    assert res["verification"]["items"][2]["label"] == "file_changes"
    assert res["verification"]["items"][2]["status"] == "warning"


def test_verify_result_failed_with_changes():
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "demo"},
            "result": {
                "status": "failure",
                "files_changed": ["partial.py"],
                "test_results": [],
            },
        }
    )
    res = verify_result(state)
    # Failed worker status makes it failed overall, but check file_changes warning
    assert res["verification"]["status"] == "failed"
    # Find file_changes item
    file_changes = next(i for i in res["verification"]["items"] if i["label"] == "file_changes")
    assert file_changes["status"] == "warning"
    assert "but changed 1 files" in file_changes["message"]
