"""Unit tests for tiered and deterministic verification helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import orchestrator.verification as verification_module
from db.enums import WorkerRuntimeMode
from orchestrator.state import OrchestratorState
from workers import WorkerResult, WorkerTestResult


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
                "secrets": {"API_KEY": "secret"},
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
                "summary": "Worker completed.",
                "files_changed": ["orchestrator/verification.py"],
            },
        }
    )


@pytest.mark.anyio
async def test_run_deterministic_verification_passes_script_to_shell_worker() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="success",
        summary="Commands passed.",
    )

    status, summary, metadata = await verification_module.run_deterministic_verification(
        state,
        worker_factory={"shell": mock_worker},
    )

    assert status == "passed"
    assert "Explicit verification commands passed." in summary
    assert metadata is not None
    assert metadata["status"] == "passed"
    assert metadata["commands"] == [".venv/bin/pytest tests/unit/test_orchestrator_graph_unit.py"]
    assert metadata["passed_commands"] == [
        ".venv/bin/pytest tests/unit/test_orchestrator_graph_unit.py"
    ]

    args, _ = mock_worker.run.call_args
    request = args[0]
    assert request.runtime_mode == WorkerRuntimeMode.SHELL
    assert request.task_text == ".venv/bin/pytest tests/unit/test_orchestrator_graph_unit.py"
    assert request.secrets == {"API_KEY": "secret"}
    assert metadata.get("python_module_shadow_guard") is None


@pytest.mark.anyio
async def test_run_deterministic_verification_guards_python_m_pytest_shadowing() -> None:
    state = _state()
    state.task_spec.verification_commands = ["python3 -m pytest -q"]
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(status="success", summary="Commands passed.")

    status, summary, metadata = await verification_module.run_deterministic_verification(
        state,
        worker_factory={"shell": mock_worker},
    )

    assert status == "passed"
    assert summary == "Explicit verification commands passed."
    assert metadata is not None
    assert metadata["commands"] == ["python3 -m pytest -q"]
    assert metadata["passed_commands"] == ["python3 -m pytest -q"]
    assert metadata["python_module_shadow_guard"] == {
        "modules": ["pytest"],
        "exit_code": 97,
    }

    args, _ = mock_worker.run.call_args
    request = args[0]
    assert "[ -e pytest.py ]" in request.task_text
    assert "[ -e pytest ]" in request.task_text
    assert "exit 97" in request.task_text
    assert request.task_text.endswith("python3 -m pytest -q")


def test_python_module_shadow_guard_detects_wrapped_python_m_pytest() -> None:
    commands = ["poetry run python -m pytest tests/unit -q"]

    guard = verification_module._python_module_shadow_guard_script(commands)

    assert guard
    assert any("python -m pytest may be shadowed" in line for line in guard)


@pytest.mark.anyio
async def test_run_deterministic_verification_fails_on_worker_failure() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(
        status="failure",
        summary="Tests failed.",
    )

    status, summary, metadata = await verification_module.run_deterministic_verification(
        state,
        worker_factory={"shell": mock_worker},
    )

    assert status == "failed"
    assert "Deterministic verification failed: Tests failed." in summary
    assert metadata is not None
    assert metadata["status"] == "failed"
    assert metadata["failed_commands"] == [
        ".venv/bin/pytest tests/unit/test_orchestrator_graph_unit.py"
    ]


@pytest.mark.anyio
async def test_run_independent_verifier_returns_warning_on_timeout_if_tests_passed() -> None:
    state = _state()
    # Add passing test results
    state.result.test_results = [WorkerTestResult(name="test1", status="passed")]

    mock_worker = AsyncMock()
    mock_worker.run.side_effect = TimeoutError()

    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={"codex": mock_worker},
    )

    assert status == "warning"
    assert "but internal tests passed" in summary
    assert reason_code == "infra_verifier_unavailable"


@pytest.mark.anyio
async def test_run_independent_verifier_returns_warning_on_timeout_if_tests_failed() -> None:
    state = _state()
    # Add failing test results
    state.result.test_results = [WorkerTestResult(name="test1", status="failed")]

    mock_worker = AsyncMock()
    mock_worker.run.side_effect = TimeoutError()

    status, summary, reason_code = await verification_module.run_independent_verifier(
        state,
        worker_factory={"codex": mock_worker},
    )

    assert status == "warning"
    assert "Independent verifier timed out" in summary
    assert "but internal tests passed" not in summary
    assert reason_code == "infra_verifier_unavailable"


@pytest.mark.anyio
async def test_run_deterministic_verification_returns_warning_on_no_result() -> None:
    state = _state()
    state.result = None

    status, summary, _ = await verification_module.run_deterministic_verification(
        state, worker_factory={}
    )

    assert status == "warning"
    assert "no worker result available" in summary


@pytest.mark.anyio
async def test_run_deterministic_verification_returns_warning_on_missing_shell_worker() -> None:
    state = _state()

    status, summary, metadata = await verification_module.run_deterministic_verification(
        state, worker_factory={"codex": AsyncMock()}
    )

    assert status == "warning"
    assert "no 'shell' worker available" in summary
    assert metadata is not None
    assert metadata["status"] == "warning"
    assert metadata["commands"] == [".venv/bin/pytest tests/unit/test_orchestrator_graph_unit.py"]


@pytest.mark.anyio
async def test_run_deterministic_verification_handles_timeout_with_passing_tests() -> None:
    state = _state()
    state.result.test_results = [WorkerTestResult(name="t1", status="passed")]
    mock_worker = AsyncMock()
    mock_worker.run.side_effect = TimeoutError()

    status, summary, metadata = await verification_module.run_deterministic_verification(
        state,
        worker_factory={"shell": mock_worker},
    )

    assert status == "warning"
    assert "timed out after 90s, but internal tests passed" in summary
    assert metadata is not None
    assert metadata["status"] == "warning"


@pytest.mark.anyio
async def test_run_deterministic_verification_handles_timeout_with_failing_tests() -> None:
    state = _state()
    state.result.test_results = [WorkerTestResult(name="t1", status="failed")]
    mock_worker = AsyncMock()
    mock_worker.run.side_effect = TimeoutError()

    status, summary, metadata = await verification_module.run_deterministic_verification(
        state,
        worker_factory={"shell": mock_worker},
    )

    assert status == "failed"
    assert "timed out after 90s" in summary
    assert metadata is not None
    assert metadata["status"] == "failed"


@pytest.mark.anyio
async def test_run_deterministic_verification_handles_infra_error() -> None:
    state = _state()
    mock_worker = AsyncMock()
    mock_worker.run.side_effect = RuntimeError("Broken")

    status, summary, metadata = await verification_module.run_deterministic_verification(
        state,
        worker_factory={"shell": mock_worker},
    )

    assert status == "failed"
    assert "infrastructure error: RuntimeError" in summary
    assert metadata is not None
    assert metadata["status"] == "failed"


@pytest.mark.anyio
async def test_run_deterministic_verification_returns_passed_on_no_commands() -> None:
    state = _state()
    state.task_spec.verification_commands = []

    status, summary, _ = await verification_module.run_deterministic_verification(
        state, worker_factory={}
    )

    assert status == "passed"
    assert "No explicit verification commands defined" in summary


@pytest.mark.anyio
async def test_run_deterministic_verification_applies_diff_text() -> None:
    state = _state()
    state.result.diff_text = "test diff"
    mock_worker = AsyncMock()
    mock_worker.run.return_value = WorkerResult(status="success", summary="done")

    await verification_module.run_deterministic_verification(
        state,
        worker_factory={"shell": mock_worker},
    )

    args, _ = mock_worker.run.call_args
    request = args[0]
    assert request.constraints["apply_diff_text"] == "test diff"


def test_resolve_verification_commands_falls_back_to_constraints() -> None:
    state = _state()
    state.task_spec = None
    state.task.constraints["verification_commands"] = ["fallback-cmd"]

    cmds = verification_module.resolve_verification_commands(state)
    assert cmds == ["fallback-cmd"]


def test_resolve_verification_commands_prefers_task_spec() -> None:
    state = _state()
    state.task_spec.verification_commands = ["task-cmd"]
    state.task.constraints["verification_commands"] = ["ignored-cmd"]

    cmds = verification_module.resolve_verification_commands(state)
    assert cmds == ["task-cmd"]


def test_split_verification_commands_filters_placeholder_templates() -> None:
    placeholder = (
        "<project-specific smoke test command that exercises the task and confirms only "
        "PWD and HOME are emitted>"
    )
    executable, placeholders = verification_module.split_verification_commands(
        [
            "pwd",
            placeholder,
            "printf '%s\\n' \"$HOME\"",
        ]
    )
    assert executable == ["pwd", "printf '%s\\n' \"$HOME\""]
    assert placeholders == [placeholder]


@pytest.mark.anyio
async def test_run_deterministic_verification_skips_placeholder_only_commands() -> None:
    state = _state()
    placeholder = (
        "<project-specific smoke test command that exercises the task and confirms only "
        "PWD and HOME are emitted>"
    )
    state.task_spec.verification_commands = [placeholder]
    mock_worker = AsyncMock()

    status, summary, metadata = await verification_module.run_deterministic_verification(
        state,
        worker_factory={"shell": mock_worker},
    )

    assert status == "warning"
    assert "placeholder" in summary.lower()
    assert metadata is not None
    assert metadata["skip_reason_code"] == "verification_commands_placeholder_only"
    assert metadata["placeholder_count"] == 1
    assert metadata["commands"] == []
    assert metadata["status"] == "warning"
    mock_worker.run.assert_not_called()
