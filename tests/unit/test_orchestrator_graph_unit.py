"""Unit tests for the orchestrator graph internals."""

from orchestrator.checkpoints import create_in_memory_checkpointer
from orchestrator.graph import (
    _build_worker_request,
    _classify_task_kind,
    _coerce_approval_decision,
    _default_worker_result_provider,
    _ensure_state,
    _is_destructive_task,
    await_approval,
    choose_worker,
    summarize_result,
)
from orchestrator.state import OrchestratorState
from workers import WorkerRequest


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


def test_choose_worker_override():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo", "worker_override": "codex"}}
    )
    res = choose_worker(state)
    assert res["route"]["chosen_worker"] == "codex"


def test_choose_worker_architecture_default():
    state = OrchestratorState.model_validate(
        {"task": {"task_text": "demo"}, "task_kind": "architecture"}
    )
    res = choose_worker(state)
    assert res["route"]["chosen_worker"] == "claude"


def test_await_approval_not_required():
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    state.approval.required = False
    res = await_approval(state)
    assert res["current_step"] == "await_approval"


def test_summarize_result_no_result():
    state = OrchestratorState.model_validate({"task": {"task_text": "demo"}})
    res = summarize_result(state)
    assert res["result"]["status"] == "error"


def test_create_in_memory_checkpointer():
    cp = create_in_memory_checkpointer()
    assert cp is not None
