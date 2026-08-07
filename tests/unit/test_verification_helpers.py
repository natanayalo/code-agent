"""Comprehensive unit tests for orchestrator/verification.py helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from orchestrator.state import OrchestratorState
from orchestrator.verification import (
    _build_verifier_task_text,
    _coerce_outcome_status,
    _deterministic_exception_result,
    _deterministic_timeout_result,
    _deterministic_worker_failure_result,
    _extract_json_payload,
    _fallback_status_from_text,
    _get_verifier_workers,
    _handle_verifier_worker_exception,
    _handle_verifier_worker_failure,
    _internal_tests_passed,
    _is_placeholder_verification_command,
    _last_command_exit_code,
    _looks_like_python_executable,
    _normalize_verification_commands,
    _parse_verifier_result,
    _python_module_invocations,
    _python_module_shadow_guard_script,
    _resolve_independent_verifier_timeout_seconds,
    _shadow_guard_modules_for_commands,
    _with_deterministic_command_metadata,
    _with_python_module_shadow_guard_metadata,
    _worker_stream,
    resolve_verification_commands,
    split_verification_commands,
)
from workers import WorkerCommand, WorkerResult

# ---------------------------------------------------------------------------
# _normalize_verification_commands
# ---------------------------------------------------------------------------


def test_normalize_verification_commands_string_single():
    result = _normalize_verification_commands("pytest tests/")
    assert result == ["pytest tests/"]


def test_normalize_verification_commands_string_multiline():
    raw = "pytest tests/\nnpm test"
    result = _normalize_verification_commands(raw)
    assert result == ["pytest tests/", "npm test"]


def test_normalize_verification_commands_string_continuation():
    raw = "pytest \\\n  tests/"
    result = _normalize_verification_commands(raw)
    assert result == ["pytest tests/"]


def test_normalize_verification_commands_string_empty_lines():
    raw = "pytest\n\nnpm test"
    result = _normalize_verification_commands(raw)
    assert result == ["pytest", "npm test"]


def test_normalize_verification_commands_list():
    result = _normalize_verification_commands(["pytest", "  npm test  ", ""])
    assert result == ["pytest", "npm test"]


def test_normalize_verification_commands_list_with_non_string():
    result = _normalize_verification_commands(["pytest", 42, None])
    assert result == ["pytest"]


def test_normalize_verification_commands_tuple():
    result = _normalize_verification_commands(("pytest",))
    assert result == ["pytest"]


def test_normalize_verification_commands_none():
    assert _normalize_verification_commands(None) == []


def test_normalize_verification_commands_int():
    assert _normalize_verification_commands(42) == []


def test_normalize_verification_commands_trailing_continuation():
    raw = "pytest tests/ \\"
    result = _normalize_verification_commands(raw)
    # Trailing continuation line is flushed at end
    assert "pytest tests/" in result[0]


# ---------------------------------------------------------------------------
# resolve_verification_commands
# ---------------------------------------------------------------------------


def test_resolve_verification_commands_from_task_spec():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
    )
    state.task_spec = MagicMock()
    state.task_spec.verification_commands = ["pytest"]
    result = resolve_verification_commands(state)
    assert "pytest" in result


def test_resolve_verification_commands_from_constraints():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {"verification_commands": "pytest tests/"},
        },
    )
    result = resolve_verification_commands(state)
    assert "pytest tests/" in result


def test_resolve_verification_commands_post_worker_combined():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {
                "verification_commands": ["pytest"],
                "operator_post_worker_verification_commands": ["mypy ."],
            },
        },
    )
    result = resolve_verification_commands(state)
    assert "pytest" in result
    assert "mypy ." in result


def test_resolve_verification_commands_no_spec():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = resolve_verification_commands(state)
    assert result == []


# ---------------------------------------------------------------------------
# _is_placeholder_verification_command
# ---------------------------------------------------------------------------


def test_is_placeholder_empty():
    assert _is_placeholder_verification_command("") is False


def test_is_placeholder_angle_brackets():
    assert _is_placeholder_verification_command("<run tests>") is True


def test_is_placeholder_project_specific():
    assert _is_placeholder_verification_command("<project-specific test command>") is True
    assert _is_placeholder_verification_command("<project specific check>") is True


def test_is_placeholder_real_command():
    assert _is_placeholder_verification_command("pytest tests/") is False


# ---------------------------------------------------------------------------
# split_verification_commands
# ---------------------------------------------------------------------------


def test_split_verification_commands():
    commands = ["pytest", "<run linting>", "mypy .", "<project-specific check>"]
    executable, placeholders = split_verification_commands(commands)
    assert executable == ["pytest", "mypy ."]
    assert len(placeholders) == 2


# ---------------------------------------------------------------------------
# _looks_like_python_executable
# ---------------------------------------------------------------------------


def test_looks_like_python_executable():
    assert _looks_like_python_executable("python") is True
    assert _looks_like_python_executable("python3") is True
    assert _looks_like_python_executable("python3.12") is True
    assert _looks_like_python_executable("/usr/bin/python3") is True
    assert _looks_like_python_executable("node") is False
    assert _looks_like_python_executable("pytest") is False


# ---------------------------------------------------------------------------
# _python_module_invocations
# ---------------------------------------------------------------------------


def test_python_module_invocations_basic():
    result = _python_module_invocations("python -m pytest tests/")
    assert "pytest" in result


def test_python_module_invocations_no_module():
    result = _python_module_invocations("python script.py")
    assert result == []


def test_python_module_invocations_with_colon():
    result = _python_module_invocations("python3 -m pytest:tests tests/")
    assert "pytest" in result


def test_python_module_invocations_invalid_shell():
    # shlex.split fails on unmatched quotes
    result = _python_module_invocations("python -m 'unclosed")
    assert result == []


def test_python_module_invocations_flag_before_m():
    result = _python_module_invocations("python -q -m pytest")
    assert "pytest" in result


def test_python_module_invocations_dash_c():
    result = _python_module_invocations("python -c 'print(1)'")
    assert result == []


# ---------------------------------------------------------------------------
# _shadow_guard_modules_for_commands
# ---------------------------------------------------------------------------


def test_shadow_guard_modules_for_pytest():
    commands = ["python -m pytest tests/"]
    result = _shadow_guard_modules_for_commands(commands)
    assert "pytest" in result


def test_shadow_guard_no_guarded_modules():
    commands = ["python -m mypy ."]
    result = _shadow_guard_modules_for_commands(commands)
    assert result == []


# ---------------------------------------------------------------------------
# _python_module_shadow_guard_script
# ---------------------------------------------------------------------------


def test_python_module_shadow_guard_script_with_pytest():
    commands = ["python -m pytest tests/"]
    script = _python_module_shadow_guard_script(commands)
    assert len(script) > 0
    assert any("pytest" in line for line in script)


def test_python_module_shadow_guard_script_no_guarded():
    commands = ["npm test"]
    script = _python_module_shadow_guard_script(commands)
    assert script == []


# ---------------------------------------------------------------------------
# _with_python_module_shadow_guard_metadata
# ---------------------------------------------------------------------------


def test_with_python_module_shadow_guard_metadata_adds():
    commands = ["python -m pytest tests/"]
    result = _with_python_module_shadow_guard_metadata(None, commands)
    assert result is not None
    assert "python_module_shadow_guard" in result


def test_with_python_module_shadow_guard_metadata_no_match():
    commands = ["npm test"]
    result = _with_python_module_shadow_guard_metadata({"existing": True}, commands)
    assert result == {"existing": True}


# ---------------------------------------------------------------------------
# _resolve_independent_verifier_timeout_seconds
# ---------------------------------------------------------------------------


def test_resolve_independent_verifier_timeout_seconds_default():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    timeout = _resolve_independent_verifier_timeout_seconds(state)
    assert timeout > 0


def test_resolve_independent_verifier_timeout_seconds_from_budget():
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "budget": {"independent_verifier_timeout_seconds": 120},
        }
    )
    timeout = _resolve_independent_verifier_timeout_seconds(state)
    assert timeout == 120


# ---------------------------------------------------------------------------
# _extract_json_payload
# ---------------------------------------------------------------------------


def test_extract_json_payload_valid():
    summary = '{"status": "passed", "summary": "All tests pass"}'
    result = _extract_json_payload(summary)
    assert result is not None
    assert result["status"] == "passed"


def test_extract_json_payload_invalid_json():
    result = _extract_json_payload("not json at all")
    assert result is None


def test_extract_json_payload_non_dict():
    result = _extract_json_payload("[1, 2, 3]")
    assert result is None


def test_extract_json_payload_empty():
    result = _extract_json_payload("")
    assert result is None


# ---------------------------------------------------------------------------
# _coerce_outcome_status
# ---------------------------------------------------------------------------


def test_coerce_outcome_status_passed():
    assert _coerce_outcome_status("passed") == "passed"
    assert _coerce_outcome_status("success") == "passed"


def test_coerce_outcome_status_failed():
    assert _coerce_outcome_status("failed") == "failed"
    assert _coerce_outcome_status("failure") == "failed"
    assert _coerce_outcome_status("error") == "failed"


def test_coerce_outcome_status_warning():
    assert _coerce_outcome_status("warning") == "warning"


def test_coerce_outcome_status_unknown():
    assert _coerce_outcome_status("unknown_status") is None
    assert _coerce_outcome_status(42) is None
    assert _coerce_outcome_status(None) is None


def test_coerce_outcome_status_case_insensitive():
    assert _coerce_outcome_status("PASSED") == "passed"
    assert _coerce_outcome_status("Failed") == "failed"


# ---------------------------------------------------------------------------
# _fallback_status_from_text
# ---------------------------------------------------------------------------


def test_fallback_status_from_text_failed():
    assert _fallback_status_from_text("test failed") == "failed"
    assert _fallback_status_from_text("regression found") == "failed"
    assert _fallback_status_from_text("error occurred") == "failed"


def test_fallback_status_from_text_passed():
    assert _fallback_status_from_text("all tests pass") == "passed"
    assert _fallback_status_from_text("ok everything") == "passed"
    assert _fallback_status_from_text("success") == "passed"


def test_fallback_status_from_text_warning():
    assert _fallback_status_from_text("something unclear") == "warning"


# ---------------------------------------------------------------------------
# _parse_verifier_result
# ---------------------------------------------------------------------------


def test_parse_verifier_result_from_json_payload():
    result = WorkerResult(
        status="success",
        summary="outer",
        json_payload={"status": "passed", "summary": "All checks passed"},
    )
    status, message = _parse_verifier_result(result)
    assert status == "passed"
    assert "All checks passed" in message


def test_parse_verifier_result_from_summary_json():
    result = WorkerResult(
        status="success",
        summary='{"status": "failed", "summary": "Tests failed"}',
    )
    status, message = _parse_verifier_result(result)
    assert status == "failed"


def test_parse_verifier_result_fallback():
    result = WorkerResult(status="success", summary="All good, tests passed!")
    status, message = _parse_verifier_result(result)
    assert status == "passed"


def test_parse_verifier_result_no_summary():
    result = WorkerResult(status="success", summary=None)
    status, message = _parse_verifier_result(result)
    assert status in ("passed", "failed", "warning")
    assert "no summary" in message


def test_parse_verifier_result_json_no_status():
    result = WorkerResult(
        status="success",
        json_payload={"message": "stuff"},
    )
    # Falls back to text heuristic
    status, message = _parse_verifier_result(result)
    assert status in ("passed", "failed", "warning")


def test_parse_verifier_result_json_status_no_message():
    result = WorkerResult(
        status="success",
        json_payload={"status": "passed"},
    )
    status, message = _parse_verifier_result(result)
    assert status == "passed"
    assert "without a summary" in message


# ---------------------------------------------------------------------------
# _internal_tests_passed
# ---------------------------------------------------------------------------


def test_internal_tests_passed_no_result():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    assert _internal_tests_passed(state) is False


def test_internal_tests_passed_success_no_tests():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success"),
    )
    assert _internal_tests_passed(state) is True


def test_internal_tests_passed_failure_no_tests():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="failure"),
    )
    assert _internal_tests_passed(state) is False


# ---------------------------------------------------------------------------
# _get_verifier_workers
# ---------------------------------------------------------------------------


def test_get_verifier_workers_empty():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = _get_verifier_workers(state, {})
    assert result == []


def test_get_verifier_workers_priority_order():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    factory = {
        "codex": MagicMock(),
        "antigravity": MagicMock(),
        "openrouter": MagicMock(),
        "shell": MagicMock(),  # should be excluded
    }
    result = _get_verifier_workers(state, factory)
    names = [name for name, _ in result]
    assert names[0] == "antigravity"
    assert "shell" not in names


def test_get_verifier_workers_dispatch_worker_included():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
    )
    state.dispatch = state.dispatch.model_copy(update={"worker_type": "gemini"})
    gemini_worker = MagicMock()
    result = _get_verifier_workers(state, {"gemini": gemini_worker})
    names = [name for name, _ in result]
    assert "gemini" in names


# ---------------------------------------------------------------------------
# _handle_verifier_worker_failure
# ---------------------------------------------------------------------------


def test_handle_verifier_worker_failure_infra_not_last():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = WorkerResult(status="failure", failure_kind="provider_error", summary="rate limit")
    status, msg, code = _handle_verifier_worker_failure(
        result, "codex", state, is_last_worker=False
    )
    assert status is None


def test_handle_verifier_worker_failure_infra_last():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = WorkerResult(status="failure", failure_kind="timeout", summary="timed out")
    status, msg, code = _handle_verifier_worker_failure(result, "codex", state, is_last_worker=True)
    assert status == "warning"
    assert code == "infra_verifier_unavailable"


def test_handle_verifier_worker_failure_non_infra():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    result = WorkerResult(status="failure", failure_kind="test", summary="oops")
    status, msg, code = _handle_verifier_worker_failure(
        result, "codex", state, is_last_worker=False
    )
    assert status == "warning"
    assert code == "infra_verifier_unavailable"


# ---------------------------------------------------------------------------
# _handle_verifier_worker_exception
# ---------------------------------------------------------------------------


def test_handle_verifier_worker_exception_timeout_not_last():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    status, msg, code = _handle_verifier_worker_exception(
        TimeoutError(), "codex", state, is_last_worker=False
    )
    assert status is None


def test_handle_verifier_worker_exception_timeout_last_tests_passed():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success"),
    )
    status, msg, code = _handle_verifier_worker_exception(
        TimeoutError(), "codex", state, is_last_worker=True
    )
    assert status == "warning"
    assert "internal tests passed" in msg


def test_handle_verifier_worker_exception_timeout_last_no_tests():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="failure"),
    )
    status, msg, code = _handle_verifier_worker_exception(
        TimeoutError(), "codex", state, is_last_worker=True
    )
    assert status == "warning"
    assert code == "infra_verifier_unavailable"


def test_handle_verifier_worker_exception_other_not_last():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    status, msg, code = _handle_verifier_worker_exception(
        RuntimeError("boom"), "codex", state, is_last_worker=False
    )
    assert status is None


def test_handle_verifier_worker_exception_other_last():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    status, msg, code = _handle_verifier_worker_exception(
        RuntimeError("boom"), "codex", state, is_last_worker=True
    )
    assert status == "warning"
    assert code == "infra_verifier_unavailable"


# ---------------------------------------------------------------------------
# _with_deterministic_command_metadata
# ---------------------------------------------------------------------------


def test_with_deterministic_command_metadata_passed():
    result = _with_deterministic_command_metadata(
        None, commands=["pytest"], status="passed", summary="All tests pass"
    )
    assert result["status"] == "passed"
    assert result["passed_commands"] == ["pytest"]
    assert "failed_commands" not in result


def test_with_deterministic_command_metadata_failed():
    result = _with_deterministic_command_metadata(
        {"extra": True},
        commands=["pytest"],
        status="failed",
        summary="Tests failed",
        exit_code=1,
        stdout="output",
        stderr="error",
    )
    assert result["status"] == "failed"
    assert result["failed_commands"] == ["pytest"]
    assert result["exit_code"] == 1
    assert "stdout_preview" in result
    assert "stderr_preview" in result
    assert result["extra"] is True


# ---------------------------------------------------------------------------
# _deterministic_timeout_result
# ---------------------------------------------------------------------------


def test_deterministic_timeout_result_tests_passed():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success"),
    )
    status, summary, meta = _deterministic_timeout_result(
        state, timeout_seconds=30, metadata=None, commands=["pytest"]
    )
    assert status == "warning"
    assert "internal tests passed" in summary


def test_deterministic_timeout_result_tests_failed():
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="failure"),
    )
    status, summary, meta = _deterministic_timeout_result(
        state, timeout_seconds=30, metadata=None, commands=["pytest"]
    )
    assert status == "failed"


# ---------------------------------------------------------------------------
# _deterministic_exception_result
# ---------------------------------------------------------------------------


def test_deterministic_exception_result():
    status, summary, meta = _deterministic_exception_result(
        RuntimeError("infra broken"), metadata=None, commands=["pytest"]
    )
    assert status == "failed"
    assert "RuntimeError" in summary


# ---------------------------------------------------------------------------
# _deterministic_worker_failure_result
# ---------------------------------------------------------------------------


def test_deterministic_worker_failure_result():
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    verifier_result = WorkerResult(status="failure", summary="pytest failed")
    status, summary, meta = _deterministic_worker_failure_result(
        verifier_result, state, metadata=None, commands=["pytest"]
    )
    assert status == "failed"
    assert "pytest failed" in summary


# ---------------------------------------------------------------------------
# _last_command_exit_code / _worker_stream
# ---------------------------------------------------------------------------


def test_last_command_exit_code():
    result = WorkerResult(
        status="success",
        commands_run=[
            WorkerCommand(command="pytest", exit_code=0),
            WorkerCommand(command="mypy", exit_code=1),
        ],
    )
    assert _last_command_exit_code(result) == 1


def test_last_command_exit_code_empty():
    result = WorkerResult(status="success")
    assert _last_command_exit_code(result) is None


def test_worker_stream():
    assert _worker_stream("hello") == "hello"
    assert _worker_stream(None) is None
    assert _worker_stream(42) is None


# ---------------------------------------------------------------------------
# _build_verifier_task_text
# ---------------------------------------------------------------------------


def test_build_verifier_task_text():
    state = OrchestratorState(
        task={"task_text": "implement feature X", "repo_url": "url"},
        result=WorkerResult(status="success", summary="Done", files_changed=["src/main.py"]),
    )
    text = _build_verifier_task_text(state)
    assert "implement feature X" in text or "Independently verify" in text
