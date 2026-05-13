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


def test_normalize_verification_commands_preserves_line_continuations() -> None:
    commands = verification_module._normalize_verification_commands(  # noqa: SLF001
        "pytest \\\n -v tests/unit/test_orchestrator_verification.py\n\nruff check"
    )

    assert commands == [
        "pytest -v tests/unit/test_orchestrator_verification.py",
        "ruff check",
    ]


@pytest.mark.anyio
async def test_run_independent_verifier_uses_native_read_only_request() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="success",
        summary='{"status":"passed","summary":"all checks passed"}',
    )

    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={"codex": mock_worker},
    )

    assert status == "passed"
    assert summary == "all checks passed"
    assert reason_code is None

    args, kwargs = mock_worker.run.call_args
    request = args[0]
    assert request.constraints["read_only"] is True
    assert request.runtime_mode == WorkerRuntimeMode.NATIVE_AGENT
    assert request.budget["worker_timeout_seconds"] == 90
    assert "strict read-only mode" in request.task_text


@pytest.mark.anyio
async def test_run_independent_verifier_parses_fenced_json_summary() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="success",
        summary='```json\n{"status":"warning","summary":"could not run full suite"}\n```',
    )

    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={"gemini": mock_worker},
    )

    assert status == "warning"
    assert summary == "could not run full suite"
    assert reason_code is None


@pytest.mark.anyio
async def test_run_independent_verifier_parses_multiple_fenced_blocks() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="success",
        summary=(
            "```text\nlogs...\n```\n"
            '```json\n{"status":"passed","summary":"structured payload"}\n```'
        ),
    )

    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={"gemini": mock_worker},
    )

    assert status == "passed"
    assert summary == "structured payload"
    assert reason_code is None


@pytest.mark.anyio
async def test_run_independent_verifier_infrastructure_exception_is_warning() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.side_effect = RuntimeError("boom")

    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={"codex": mock_worker},
    )

    assert status == "warning"
    assert "infrastructure error" in summary
    assert reason_code == "infra_verifier_unavailable"


@pytest.mark.anyio
async def test_run_independent_verifier_reports_warning_when_worker_missing() -> None:
    state = _state()

    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={},
    )

    assert status == "warning"
    assert "no verifier worker configured" in summary
    assert reason_code == "no_verifier_worker"


@pytest.mark.anyio
async def test_run_independent_verifier_no_result_returns_warning() -> None:
    state = _state()
    state.result = None
    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={"codex": AsyncMock()},
    )
    assert status == "warning"
    assert "no worker result available" in summary
    assert reason_code == "no_result"


@pytest.mark.anyio
async def test_run_independent_verifier_non_success_timeout_failure_kind_maps_to_warning() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="failure",
        failure_kind="timeout",
        summary="timed out",
    )
    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={"codex": mock_worker},
    )
    assert status == "warning"
    assert "could not complete" in summary
    assert reason_code == "infra_verifier_unavailable"


def test_parse_verifier_result_falls_back_to_text_summary() -> None:
    status, summary = verification_module._parse_verifier_result(  # noqa: SLF001
        WorkerResult(status="success", summary="looks good overall")
    )
    assert status in {"passed", "warning"}
    assert "unstructured output" in summary
