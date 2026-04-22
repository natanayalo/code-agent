import json
import subprocess
from unittest.mock import MagicMock, patch

from workers.base import WorkerCommand
from workers.cli_runtime import CliRuntimeBudgetLedger, CliRuntimeSettings
from workers.self_review import (
    _extract_json_object,
    build_self_review_prompt,
    build_targeted_review_context_packet,
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


def test_build_targeted_review_context_packet_focuses_on_changed_code(tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        "\n".join(
            [
                "def alpha():",
                "    return 1",
                "",
                "def beta():",
                "    value = alpha()",
                "    return value + 1",
            ]
        )
    )
    diff_text = "\n".join(
        [
            "diff --git a/module.py b/module.py",
            "index 1111111..2222222 100644",
            "--- a/module.py",
            "+++ b/module.py",
            "@@ -3,2 +3,3 @@",
            " def beta():",
            "+    print('x')",
            "     value = alpha()",
            "     return value + 1",
        ]
    )

    packet = build_targeted_review_context_packet(
        task_text="Update beta behavior",
        worker_summary="Added logging for debugging.",
        files_changed=["module.py"],
        diff_text=diff_text,
        repo_path=tmp_path,
        commands_run=[WorkerCommand(command="pytest -q", exit_code=0)],
        verifier_report={"outcome": "ok"},
        session_state={"active_goal": "Finish module.py"},
    )

    assert "### Task Objective" in packet
    assert "### Changed Files" in packet
    assert "- module.py" in packet
    assert "### Command Summary" in packet
    assert "pytest -q" in packet
    assert "### Changed-File Code Windows" in packet
    assert "0004: def beta():" in packet
    assert "0005:     value = alpha()" in packet


def test_build_targeted_review_context_packet_respects_character_budget(tmp_path):
    source = tmp_path / "big.py"
    source.write_text("\n".join([f"line_{index}" for index in range(1, 200)]))
    diff_text = "\n".join(
        [
            "diff --git a/big.py b/big.py",
            "index 1111111..2222222 100644",
            "--- a/big.py",
            "+++ b/big.py",
            "@@ -1,1 +1,20 @@",
            *[f"+line_{index}" for index in range(1, 50)],
        ]
    )
    packet = build_targeted_review_context_packet(
        task_text="x" * 600,
        worker_summary="y" * 600,
        files_changed=["big.py"],
        diff_text=diff_text,
        repo_path=tmp_path,
        max_characters=350,
    )

    assert len(packet) <= 420
    assert "truncated" in packet


def test_build_self_review_prompt_includes_review_context_packet(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("def run():\n    return 1\n")
    prompt = build_self_review_prompt(
        task_text="Update run() behavior",
        worker_summary="Updated return value.",
        files_changed=["main.py"],
        diff_text=(
            "diff --git a/main.py b/main.py\n"
            "--- a/main.py\n"
            "+++ b/main.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-def run():\n"
            "+def run():\n"
        ),
        repo_path=tmp_path,
        commands_run=[WorkerCommand(command="pytest tests/unit", exit_code=1)],
    )

    assert "## Review Context Packet" in prompt
    assert "### Diff Excerpt" in prompt
    assert "pytest tests/unit" in prompt
