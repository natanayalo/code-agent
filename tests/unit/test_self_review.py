import json
import re
import subprocess
from unittest.mock import MagicMock, patch

import workers.self_review as self_review
from tools import DEFAULT_TOOL_REGISTRY, ToolPermissionLevel
from workers.base import WorkerCommand
from workers.cli_runtime import (
    CliRuntimeBudgetLedger,
    CliRuntimeExecutionResult,
    CliRuntimeMessage,
    CliRuntimeSettings,
    CliRuntimeStep,
)
from workers.self_review import (
    _extract_diff_line_hints,
    _extract_json_object,
    build_self_review_prompt,
    build_targeted_review_context_packet,
    collect_diff_for_review,
    merge_budget_ledgers,
    parse_review_result,
    remaining_runtime_settings,
    run_shared_self_review_fix_loop,
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


def test_build_targeted_review_context_packet_includes_deletion_anchor_window(tmp_path):
    source = tmp_path / "delete_case.py"
    source.write_text(
        "\n".join(
            [
                "def before():",
                "    return 1",
                "",
                "def removed():",
                "    return 2",
                "",
                "def after():",
                "    return 3",
            ]
        )
    )
    diff_text = "\n".join(
        [
            "diff --git a/delete_case.py b/delete_case.py",
            "index 1111111..2222222 100644",
            "--- a/delete_case.py",
            "+++ b/delete_case.py",
            "@@ -4,2 +4,0 @@",
            "-def removed():",
            "-    return 2",
        ]
    )

    packet = build_targeted_review_context_packet(
        task_text="Remove dead code",
        worker_summary="Removed the obsolete helper.",
        files_changed=["delete_case.py"],
        diff_text=diff_text,
        repo_path=tmp_path,
    )

    assert "delete_case.py (1-20)" not in packet
    assert "delete_case.py (" in packet
    assert "0004: def removed():" in packet


def test_build_targeted_review_context_packet_uses_disjoint_windows(tmp_path):
    source = tmp_path / "disjoint.py"
    source.write_text("\n".join([f"line_{index}" for index in range(1, 61)]))
    diff_text = "\n".join(
        [
            "diff --git a/disjoint.py b/disjoint.py",
            "index 1111111..2222222 100644",
            "--- a/disjoint.py",
            "+++ b/disjoint.py",
            "@@ -5,1 +5,1 @@",
            "-line_5",
            "+line_5_changed",
            "@@ -50,1 +50,1 @@",
            "-line_50",
            "+line_50_changed",
        ]
    )

    packet = build_targeted_review_context_packet(
        task_text="Update two distant lines",
        worker_summary="Applied two focused edits.",
        files_changed=["disjoint.py"],
        diff_text=diff_text,
        repo_path=tmp_path,
    )

    assert "disjoint.py (2-8)" in packet
    assert "disjoint.py (47-53)" in packet


def test_build_targeted_review_context_packet_enforces_code_line_budget(tmp_path):
    source = tmp_path / "budget.py"
    source.write_text("\n".join([f"line_{index}" for index in range(1, 401)]))
    diff_text = "\n".join(
        [
            "diff --git a/budget.py b/budget.py",
            "index 1111111..2222222 100644",
            "--- a/budget.py",
            "+++ b/budget.py",
            "@@ -1,1 +1,200 @@",
            *[f"+line_{index}" for index in range(1, 201)],
        ]
    )

    packet = build_targeted_review_context_packet(
        task_text="Large insert",
        worker_summary="Added many lines.",
        files_changed=["budget.py"],
        diff_text=diff_text,
        repo_path=tmp_path,
    )

    code_lines = [line for line in packet.splitlines() if re.match(r"^\d{4}:", line)]
    assert len(code_lines) <= 120
    assert "window truncated to respect 120-line packet budget" in packet


def test_extract_diff_line_hints_normalizes_prefixed_and_quoted_paths():
    diff_text = "\n".join(
        [
            '+++ "b/folder/file name.py"',
            "@@ -4,1 +4,2 @@",
            "+++ plain/path.py",
            "@@ -9,2 +9,0 @@",
            "+++ /dev/null",
            "@@ -1,1 +1,1 @@",
        ]
    )
    hints = _extract_diff_line_hints(diff_text)
    assert hints["folder/file name.py"] == [(4, 5)]
    assert hints["plain/path.py"] == [(9, 9)]
    assert "/dev/null" not in hints


def test_build_targeted_review_context_packet_uses_robust_markdown_fences(tmp_path):
    source = tmp_path / "fence.md"
    source.write_text("```inside```\nline2\nline3\n")
    diff_text = "\n".join(
        [
            "diff --git a/fence.md b/fence.md",
            "+++ b/fence.md",
            "@@ -1,1 +1,1 @@",
            "-```inside```",
            "+```inside``` updated",
        ]
    )
    packet = build_targeted_review_context_packet(
        task_text="Update fenced content",
        worker_summary="Changed markdown fence content.",
        files_changed=["fence.md"],
        diff_text=diff_text,
        repo_path=tmp_path,
    )
    assert "````diff" in packet
    assert "````text" in packet


def test_build_targeted_review_context_packet_uses_dynamic_markdown_fences(tmp_path):
    source = tmp_path / "fence5.md"
    source.write_text("````inside````\nline2\n")
    diff_text = "\n".join(
        [
            "diff --git a/fence5.md b/fence5.md",
            "+++ b/fence5.md",
            "@@ -1,1 +1,1 @@",
            "-````inside````",
            "+````inside```` updated",
        ]
    )

    packet = build_targeted_review_context_packet(
        task_text="Update fenced content with four-backtick literal",
        worker_summary="Changed markdown fence content.",
        files_changed=["fence5.md"],
        diff_text=diff_text,
        repo_path=tmp_path,
    )

    assert "`````diff" in packet
    assert "`````text" in packet


def test_build_targeted_review_context_packet_preserves_diff_fence_when_diff_is_truncated(tmp_path):
    source = tmp_path / "long.md"
    source.write_text("line\n")
    diff_text = "\n".join(
        [
            "diff --git a/long.md b/long.md",
            "+++ b/long.md",
            "@@ -1,1 +1,1 @@",
            *["+" + ("`" * 5) + " payload line" for _ in range(40)],
        ]
    )

    packet = build_targeted_review_context_packet(
        task_text="large diff",
        worker_summary="summary",
        files_changed=["long.md"],
        diff_text=diff_text,
        repo_path=tmp_path,
        max_characters=220,
    )

    assert "``````diff" in packet
    assert packet.count("``````") >= 2


def test_truncate_review_packet_ignores_non_diff_fences_for_balance():
    packet = "\n".join(
        [
            "### Task Objective",
            "Contains ``` marker in task section.",
            "",
            "### Diff Excerpt",
            "```diff",
            "@@ -1,1 +1,1 @@",
            "-old",
            "+new",
            "```",
            "",
            "### Changed-File Code Windows",
            "extra content",
        ]
    )

    cutoff = packet.index("### Changed-File Code Windows") + 5
    truncated = self_review._truncate_review_packet(packet, cutoff, diff_fence="```")
    assert truncated.count("```") == 3


def test_build_targeted_review_context_packet_skips_oversized_files(tmp_path, monkeypatch):
    monkeypatch.setattr(self_review, "DEFAULT_REVIEW_PACKET_MAX_FILE_BYTES", 10)
    source = tmp_path / "big_payload.txt"
    source.write_text("x" * 128)
    diff_text = "\n".join(
        [
            "diff --git a/big_payload.txt b/big_payload.txt",
            "+++ b/big_payload.txt",
            "@@ -1,1 +1,1 @@",
            "-x",
            "+y",
        ]
    )

    packet = build_targeted_review_context_packet(
        task_text="Tiny change but huge file",
        worker_summary="Updated one line.",
        files_changed=["big_payload.txt"],
        diff_text=diff_text,
        repo_path=tmp_path,
    )

    assert "exceeds 10-byte limit" in packet


def test_extract_diff_line_hints_unescapes_quoted_paths():
    diff_text = "\n".join(
        [
            '+++ "b/folder\\\\name \\"quoted\\".py"',
            "@@ -2,1 +2,1 @@",
        ]
    )
    hints = _extract_diff_line_hints(diff_text)
    assert 'folder\\name "quoted".py' in hints
    assert hints['folder\\name "quoted".py'] == [(2, 2)]


def _runtime_result(
    *,
    summary: str,
    status: str = "success",
    stop_reason: str = "final_answer",
) -> CliRuntimeExecutionResult:
    return CliRuntimeExecutionResult(
        status=status,
        summary=summary,
        stop_reason=stop_reason,
        commands_run=[],
        messages=[CliRuntimeMessage(role="assistant", content=summary)],
        budget_ledger=CliRuntimeBudgetLedger(max_iterations=6),
    )


def test_run_shared_self_review_fix_loop_skips_when_disabled(tmp_path):
    execution = _runtime_result(summary="Initial success.")
    adapter = MagicMock()
    review_result, files_changed, lint_result, lint_artifacts = run_shared_self_review_fix_loop(
        execution=execution,
        task_text="Do work",
        constraints={"skip_self_review": True},
        runtime_adapter=adapter,
        runtime_settings=CliRuntimeSettings(max_iterations=6, worker_timeout_seconds=60),
        system_prompt="base prompt",
        repo_path=tmp_path,
        files_changed=["a.py"],
        lint_format_result={"ran": False, "status": "skipped"},
        lint_format_artifacts=[],
        post_run_lint_collector=lambda *_: (["a.py"], {"ran": False, "status": "skipped"}, []),
        tool_registry=DEFAULT_TOOL_REGISTRY,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        session=MagicMock(),
    )

    assert review_result is None
    assert files_changed == ["a.py"]
    assert lint_result["status"] == "skipped"
    assert lint_artifacts == []
    adapter.next_step.assert_not_called()


@patch("workers.self_review.collect_diff_for_review", return_value="diff payload")
@patch("workers.self_review.run_cli_runtime_loop")
def test_run_shared_self_review_fix_loop_runs_fix_pass_and_merges_artifacts(
    mock_run_loop, _mock_collect_diff, tmp_path
):
    review_steps = [
        CliRuntimeStep(
            kind="final",
            final_output=json.dumps(
                {
                    "summary": "Missing assertion coverage.",
                    "confidence": 1.0,
                    "outcome": "findings",
                    "findings": [
                        {
                            "severity": "low",
                            "category": "test",
                            "confidence": 1.0,
                            "file_path": "tests/test_app.py",
                            "line_start": 1,
                            "line_end": 1,
                            "title": "Add focused assertion",
                            "why_it_matters": "Coverage gap",
                        }
                    ],
                }
            ),
        ),
        CliRuntimeStep(
            kind="final",
            final_output=json.dumps(
                {
                    "summary": "All issues fixed.",
                    "confidence": 1.0,
                    "outcome": "no_findings",
                    "findings": [],
                }
            ),
        ),
    ]

    class _Adapter:
        def __init__(self) -> None:
            self._steps = list(review_steps)

        def next_step(self, *_args, **_kwargs):
            return self._steps.pop(0)

    follow_up = _runtime_result(summary="Applied fix pass.")
    follow_up.commands_run = [WorkerCommand(id="test-cmd-id", command="pytest -q", exit_code=0)]
    mock_run_loop.return_value = follow_up

    def _post_run_lint_collector(_execution, existing_files):
        return (
            [*existing_files, "tests/test_app.py"],
            {"ran": True, "status": "warning", "errors": ["second pass warning"]},
            [],
        )

    execution = _runtime_result(summary="Initial success.")
    review_result, files_changed, lint_result, lint_artifacts = run_shared_self_review_fix_loop(
        execution=execution,
        task_text="Ship tiny change",
        constraints={},
        runtime_adapter=_Adapter(),
        runtime_settings=CliRuntimeSettings(max_iterations=6, worker_timeout_seconds=60),
        system_prompt="base prompt",
        repo_path=tmp_path,
        files_changed=["app.py"],
        lint_format_result={
            "ran": True,
            "status": "passed",
            "errors": [],
            "commands": [],
            "artifacts": [],
        },
        lint_format_artifacts=[],
        post_run_lint_collector=_post_run_lint_collector,
        tool_registry=DEFAULT_TOOL_REGISTRY,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        session=MagicMock(),
    )

    assert mock_run_loop.call_count == 1
    assert execution.summary == "Applied fix pass."
    assert execution.commands_run == [
        WorkerCommand(id="test-cmd-id", command="pytest -q", exit_code=0)
    ]
    assert review_result is not None
    assert review_result.outcome == "no_findings"
    assert files_changed == ["app.py", "tests/test_app.py"]
    assert lint_result["status"] == "warning"
    assert lint_result["errors"] == ["second pass warning"]
    assert lint_artifacts == []
