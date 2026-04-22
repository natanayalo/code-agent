import json
import subprocess
from unittest.mock import MagicMock, patch

from workers.cli_runtime import CliRuntimeBudgetLedger, CliRuntimeSettings
from workers.self_review import (
    _extract_json_object,
    collect_diff_for_review,
    merge_budget_ledgers,
    parse_review_result,
    remaining_runtime_settings,
    should_skip_self_review,
)


def test_should_skip_self_review():
    assert should_skip_self_review({"skip_self_review": True}) is True
    assert should_skip_self_review({"skip_self_review": "true"}) is True
    assert should_skip_self_review({"skip_self_review": "1"}) is True
    assert should_skip_self_review({"skip_self_review": "yes"}) is True
    assert should_skip_self_review({"skip_self_review": "on"}) is True
    assert should_skip_self_review({"self_review_enabled": False}) is True
    assert should_skip_self_review({"self_review_enabled": "false"}) is True
    assert should_skip_self_review({"self_review_enabled": "0"}) is True
    assert should_skip_self_review({"self_review_enabled": "no"}) is True
    assert should_skip_self_review({"self_review_enabled": "off"}) is True
    assert should_skip_self_review({}) is False
    assert (
        should_skip_self_review({"skip_self_review": False, "self_review_enabled": True}) is False
    )


@patch("subprocess.run")
def test_collect_diff_for_review(mock_run, tmp_path):
    # Timeout
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["git"], timeout=15)
    assert "timed out" in collect_diff_for_review(tmp_path)

    # OSError
    mock_run.side_effect = OSError("boom")
    assert "diff collection failed" in collect_diff_for_review(tmp_path)

    # Return code non-zero
    mock_run.side_effect = None
    mock_run.return_value = MagicMock(returncode=1, stderr="error", stdout="")
    assert "git diff failed with exit code 1" in collect_diff_for_review(tmp_path)

    # Truncated payload
    mock_run.return_value = MagicMock(returncode=0, stdout="a" * 15000, stderr="")
    result = collect_diff_for_review(tmp_path, max_characters=100)
    assert len(result) < 200
    assert "truncated" in result


def test_parse_review_result():
    # Invalid json
    assert parse_review_result("invalid") is None
    # Valid json but not dict
    assert parse_review_result("[1, 2, 3]") is None
    # Valid json but missing fields for ReviewResult validation
    assert parse_review_result('{"foo": "bar"}') is None
    # Valid
    valid_json = json.dumps(
        {"summary": "ok", "confidence": 1.0, "outcome": "no_findings", "findings": []}
    )
    result = parse_review_result(valid_json)
    assert result is not None
    assert result.outcome == "no_findings"


def test_merge_budget_ledgers():
    existing = CliRuntimeBudgetLedger(max_iterations=10)
    additional = CliRuntimeBudgetLedger(
        max_iterations=10,
        iterations_used=1,
        tool_calls_used=2,
        shell_commands_used=3,
        retries_used=4,
        wall_clock_seconds=5.0,
        failed_command_attempts={"cmd": 1},
    )
    merge_budget_ledgers(existing, additional)
    assert existing.iterations_used == 1
    assert existing.tool_calls_used == 2
    assert existing.shell_commands_used == 3
    assert existing.retries_used == 4
    assert existing.wall_clock_seconds == 5.0
    assert existing.failed_command_attempts == {"cmd": 1}


def test_remaining_runtime_settings():
    base = CliRuntimeSettings(
        max_iterations=10,
        worker_timeout_seconds=100.0,
        max_tool_calls=20,
        max_shell_commands=30,
        max_retries=40,
    )
    ledger = CliRuntimeBudgetLedger(
        max_iterations=10,
        iterations_used=2,
        tool_calls_used=5,
        shell_commands_used=10,
        retries_used=15,
        wall_clock_seconds=20.0,
    )
    remaining = remaining_runtime_settings(base, budget_ledger=ledger)
    assert remaining is not None
    assert remaining.max_iterations == 8
    assert remaining.worker_timeout_seconds == 80.0
    assert remaining.max_tool_calls == 15
    assert remaining.max_shell_commands == 20
    assert remaining.max_retries == 25

    # Exhaust iterations
    exhausted_ledger = CliRuntimeBudgetLedger(
        max_iterations=10, iterations_used=10, wall_clock_seconds=10.0
    )
    assert remaining_runtime_settings(base, budget_ledger=exhausted_ledger) is None

    # Exhaust tool calls
    exhausted_tools = CliRuntimeBudgetLedger(
        max_iterations=10, iterations_used=1, tool_calls_used=25, wall_clock_seconds=10.0
    )
    assert remaining_runtime_settings(base, budget_ledger=exhausted_tools) is None

    # Exhaust shell commands
    exhausted_shell = CliRuntimeBudgetLedger(
        max_iterations=10, iterations_used=1, shell_commands_used=35, wall_clock_seconds=10.0
    )
    assert remaining_runtime_settings(base, budget_ledger=exhausted_shell) is None

    # Exhaust retries
    exhausted_retries = CliRuntimeBudgetLedger(
        max_iterations=10, iterations_used=1, retries_used=45, wall_clock_seconds=10.0
    )
    assert remaining_runtime_settings(base, budget_ledger=exhausted_retries) is None


def test_extract_json_object():
    assert _extract_json_object("no json here") is None
    assert _extract_json_object('{"a": 1}') == '{"a": 1}'
    assert _extract_json_object('prefix {"a": 1} suffix') == '{"a": 1}'
    assert _extract_json_object('prefix {"a": "{\\"b\\": 2}"} suffix') == '{"a": "{\\"b\\": 2}"}'
    # Unclosed bracket
    assert _extract_json_object("{") is None
    # Valid json mixed with invalid json
    assert _extract_json_object('{invalid} {"a": 1}') == '{"a": 1}'
