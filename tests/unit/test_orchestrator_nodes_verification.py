from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.nodes.verification import build_verify_result_node
from orchestrator.state import OrchestratorState
from workers import WorkerResult, WorkerTestResult


def _state() -> OrchestratorState:
    return OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "t1",
            },
            "task": {"task_id": "task-1", "task_text": "Do thing", "constraints": {}},
            "dispatch": {"worker_type": "codex"},
            "task_spec": {"goal": "g", "verification_commands": []},
            "result": {"status": "success", "summary": "done", "files_changed": []},
        }
    )


@pytest.mark.anyio
async def test_verify_node_short_circuits_when_result_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state.result = None
    node = build_verify_result_node()
    response = await node(state)
    assert response["current_step"] == "verify_result"


@pytest.mark.anyio
async def test_verify_node_handles_independent_verifier_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state.task_spec.verification_commands = ["pytest -q"]  # type: ignore[union-attr]
    state.task_spec.allowed_actions = ["modify_workspace_files"]  # type: ignore[union-attr]
    state.result = WorkerResult(
        status="success",
        summary="ok",
        files_changed=["a.py"],
        test_results=[WorkerTestResult(name="t1", status="passed")],
        commands_run=[],
    )

    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_deterministic_verification",
        AsyncMock(return_value=("passed", "ok", None)),
    )

    # Test warning status
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_independent_verifier",
        AsyncMock(return_value=("warning", "infra unavailable", "infra_verifier_unavailable")),
    )
    node = build_verify_result_node(enable_independent_verifier=True)
    response = await node(state)
    assert response["verification"]["status"] == "warning"

    # Test passed status
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_independent_verifier",
        AsyncMock(return_value=("passed", "ok", None)),
    )
    node = build_verify_result_node(enable_independent_verifier=True)
    response = await node(state)
    assert response["verification"]["status"] == "passed"


@pytest.mark.anyio
async def test_verify_node_skips_independent_verifier_on_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    state.task.constraints["read_only"] = True
    state.task_spec.verification_commands = ["pytest -q"]  # type: ignore[union-attr]
    state.result = WorkerResult(
        status="success",
        summary="ok",
        files_changed=["a.py"],
        test_results=[],
        commands_run=[],
    )

    det_mock = AsyncMock(return_value=("passed", "ok", None))
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_deterministic_verification",
        det_mock,
    )

    ind_mock = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_independent_verifier",
        ind_mock,
    )

    node = build_verify_result_node(enable_independent_verifier=True)
    response = await node(state)

    # Deterministic verifier still ran
    det_mock.assert_called_once()

    # Independent verifier was skipped
    ind_mock.assert_not_called()

    # But files changed in read_only mode is treated as anomalous
    assert response["verification"]["status"] == "failed"
    file_changes_item = next(
        i for i in response["verification"]["items"] if i["label"] == "file_changes"
    )
    assert file_changes_item["status"] == "failed"
    assert file_changes_item["reason_code"] == "scope_mismatch"

    ind_item = next(
        i for i in response["verification"]["items"] if i["label"] == "independent_verifier"
    )
    assert ind_item["status"] == "passed"
    assert "intentionally skipped" in ind_item["message"]


@pytest.mark.anyio
async def test_verify_node_fully_short_circuits_for_scout_task_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scout tasks (task_spec.task_type='scout') skip ALL verification steps.

    Regression: scout tasks were erroneously running the deterministic verifier
    (which executed the default smoke `printf` command) even though they never
    modify code. The fix short-circuits the entire verify_result node for scout.
    """
    state = _state()
    state.task_spec.task_type = "scout"  # type: ignore[union-attr]
    state.task_spec.verification_commands = [  # type: ignore[union-attr]
        'printf \'%s\\n%s\\n\' "$PWD" "$HOME"'  # default smoke command for read-only tasks
    ]
    state.result = WorkerResult(
        status="success",
        summary="Scout research complete.",
        files_changed=[],
        test_results=[],
        commands_run=[],
    )

    det_mock = AsyncMock(return_value=("passed", "ok", None))
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_deterministic_verification",
        det_mock,
    )
    ind_mock = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_independent_verifier",
        ind_mock,
    )

    node = build_verify_result_node(enable_independent_verifier=True)
    response = await node(state)

    # Both verification steps must be skipped for scout tasks
    det_mock.assert_not_called()
    ind_mock.assert_not_called()

    # Node still returns a valid verify_result step response
    assert response["current_step"] == "verify_result"


@pytest.mark.anyio
async def test_verify_node_fully_short_circuits_for_scout_with_worker_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scout task with worker_override must still skip verification entirely.

    Regression: when worker_override was passed, classify_task returned
    'implementation' but the constraints (task_type=scout) were correctly
    ingested and the task_spec.task_type was persisted as 'scout'. The
    verify_result node should honour the task_spec contract and skip all
    verification regardless of the route source.
    """
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "t1",
            },
            "task": {
                "task_id": "task-scout-override",
                "task_text": "Examine this code and write a dummy proposal.",
                "constraints": {"task_type": "scout", "read_only": True},
                "worker_override": "antigravity",
            },
            "task_kind": "implementation",  # simulate classify_task returning wrong kind
            "dispatch": {"worker_type": "antigravity"},
            "task_spec": {
                "goal": "Examine this code and write a dummy proposal.",
                "task_type": "scout",
                "allowed_actions": ["read_repo_files", "run_non_destructive_checks"],
                "verification_commands": ['printf \'%s\\n%s\\n\' "$PWD" "$HOME"'],
            },
            "result": {
                "status": "success",
                "summary": "Research done.",
                "files_changed": [],
                "test_results": [],
                "commands_run": [],
            },
        }
    )

    det_mock = AsyncMock(return_value=("passed", "ok", None))
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_deterministic_verification",
        det_mock,
    )
    ind_mock = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_independent_verifier",
        ind_mock,
    )

    node = build_verify_result_node(enable_independent_verifier=True)
    response = await node(state)

    # Both verification steps must be skipped: task_spec.task_type == "scout" is authoritative
    det_mock.assert_not_called()
    ind_mock.assert_not_called()
    assert response["current_step"] == "verify_result"


@pytest.mark.anyio
async def test_verify_node_short_circuits_for_scout_via_constraints_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scout identification via constraints is the fallback when task_spec is None.

    If task_spec has not been built yet (e.g., very early failure phases or
    unusual restored states), verification should still short-circuit when
    constraints.task_type == 'scout'.
    """
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "s1",
                "user_id": "u1",
                "channel": "http",
                "external_thread_id": "t1",
            },
            "task": {
                "task_id": "task-scout-no-spec",
                "task_text": "Scout this repo.",
                "constraints": {"task_type": "scout", "read_only": True},
            },
            "dispatch": {"worker_type": "antigravity"},
            # task_spec intentionally absent
            "result": {
                "status": "success",
                "summary": "Done.",
                "files_changed": [],
                "test_results": [],
                "commands_run": [],
            },
        }
    )
    assert state.task_spec is None  # precondition

    det_mock = AsyncMock(return_value=("passed", "ok", None))
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_deterministic_verification",
        det_mock,
    )
    ind_mock = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.nodes.verification.run_independent_verifier",
        ind_mock,
    )

    node = build_verify_result_node(enable_independent_verifier=True)
    response = await node(state)

    # Constraints fallback must prevent any verification from running
    det_mock.assert_not_called()
    ind_mock.assert_not_called()
    assert response["current_step"] == "verify_result"
