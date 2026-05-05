"""Unit tests for independent verifier helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import orchestrator.verification as verification_module
from db.enums import WorkerRuntimeMode
from orchestrator.state import OrchestratorState
from workers import WorkerResult


def _state() -> OrchestratorState:
    return OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "session-1",
                "user_id": "user-1",
                "channel": "http",
                "external_thread_id": "thread-1",
            },
            "task": {
                "task_text": "Fix verifier behavior",
                "repo_url": "https://example.com/repo.git",
                "branch": "main",
                "constraints": {},
                "budget": {"independent_verifier_timeout_seconds": 90},
            },
            "task_spec": {
                "goal": "Fix verifier behavior",
                "verification_commands": [
                    ".venv/bin/pytest tests/unit/test_orchestrator_graph_unit.py"
                ],
            },
            "memory": {"personal": [], "project": [], "session": {}},
            "dispatch": {"worker_type": "codex"},
            "result": {
                "status": "success",
                "summary": "Worker completed and updated verification logic.",
                "files_changed": ["orchestrator/verification.py"],
            },
        }
    )


@pytest.mark.anyio
async def test_run_independent_verifier_uses_native_read_only_request() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="success",
        summary='{"status":"passed","summary":"all checks passed"}',
    )

    status, summary = await verification_module.run_independent_verifier(
        state,
        worker_factory={"codex": mock_worker},
    )

    assert status == "passed"
    assert summary == "all checks passed"

    args, kwargs = mock_worker.run.call_args
    request = args[0]
    assert request.constraints["read_only"] is True
    assert request.runtime_mode == WorkerRuntimeMode.NATIVE_AGENT
    assert request.budget["worker_timeout_seconds"] == 90
    assert "system_prompt" in kwargs
    assert "strict read-only mode" in kwargs["system_prompt"]


@pytest.mark.anyio
async def test_run_independent_verifier_parses_fenced_json_summary() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="success",
        summary='```json\n{"status":"warning","summary":"could not run full suite"}\n```',
    )

    status, summary = await verification_module.run_independent_verifier(
        state,
        worker_factory={"gemini": mock_worker},
    )

    assert status == "warning"
    assert summary == "could not run full suite"


@pytest.mark.anyio
async def test_run_independent_verifier_reports_warning_when_worker_missing() -> None:
    state = _state()

    status, summary = await verification_module.run_independent_verifier(
        state,
        worker_factory={},
    )

    assert status == "warning"
    assert "no verifier worker configured" in summary
