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
