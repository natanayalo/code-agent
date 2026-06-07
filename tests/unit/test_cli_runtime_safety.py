# ruff: noqa: F403, F405
"""Behavior-focused CLI runtime tests."""

from __future__ import annotations

from tests.unit.cli_runtime_support import *  # noqa: F403


def test_run_cli_runtime_loop_blocks_commands_that_require_higher_permission() -> None:
    """Commands above the granted permission level should fail before shell execution."""
    adapter = _ScriptedAdapter(
        [CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="rm -rf build")]
    )
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=1, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "permission_required"
    assert execution.permission_decision is not None
    assert execution.permission_decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert session.calls == []
    assert "Required: dangerous_shell; granted: workspace_write" in execution.summary


def test_run_cli_runtime_loop_stops_at_the_tool_call_budget() -> None:
    """Tool-call limits should stop the runtime before an extra shell execution starts."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="ls"),
        ]
    )
    session = _FakeSession({"pwd": _command_result("pwd", output="/workspace/repo\n")})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=3,
            worker_timeout_seconds=30,
            max_tool_calls=1,
        ),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "budget_exceeded"
    assert len(session.calls) == 1
    assert execution.budget_ledger.tool_calls_used == 1
    assert execution.budget_ledger.shell_commands_used == 1
    assert "tool-call budget (1)" in execution.summary


def test_run_cli_runtime_loop_honors_zero_tool_call_budget() -> None:
    """A zero tool-call budget should fail before any command executes."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
        ]
    )
    session = _FakeSession({"pwd": _command_result("pwd", output="/workspace/repo\n")})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=2,
            worker_timeout_seconds=30,
            max_tool_calls=0,
        ),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "budget_exceeded"
    assert execution.budget_ledger.tool_calls_used == 0
    assert session.calls == []
    assert "tool-call budget (0)" in execution.summary


def test_run_cli_runtime_loop_stops_at_the_retry_budget() -> None:
    """Retry limits should block repeated failing commands before another shell run."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pytest -q"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pytest -q"),
        ]
    )
    session = _FakeSession(
        {"pytest -q": _command_result("pytest -q", output="boom\n", exit_code=1)}
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=3,
            worker_timeout_seconds=30,
            max_retries=0,
        ),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "budget_exceeded"
    assert len(session.calls) == 1
    assert execution.budget_ledger.retries_used == 0
    assert "retry budget (0)" in execution.summary


def test_run_cli_runtime_loop_treats_spacing_only_command_changes_as_retries() -> None:
    """Retry detection should normalize token-equivalent commands before comparing them."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pytest   -q"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pytest -q"),
        ]
    )
    session = _FakeSession(
        {"pytest   -q": _command_result("pytest   -q", output="boom\n", exit_code=1)}
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=3,
            worker_timeout_seconds=30,
            max_retries=0,
        ),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "budget_exceeded"
    assert len(session.calls) == 1
    assert execution.budget_ledger.retries_used == 0
    assert "retry budget (0)" in execution.summary


def test_run_cli_runtime_loop_counts_interleaved_failures_toward_retry_budget() -> None:
    """Retry limits should still apply when the same failing command is retried later."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pytest -q"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pytest -q"),
        ]
    )
    session = _FakeSession(
        {
            "pytest -q": _command_result("pytest -q", output="boom\n", exit_code=1),
            "pwd": _command_result("pwd", output="/workspace/repo\n"),
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=4,
            worker_timeout_seconds=30,
            max_retries=0,
        ),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "budget_exceeded"
    assert len(session.calls) == 2
    assert execution.budget_ledger.retries_used == 0
    assert execution.budget_ledger.failed_command_attempts == {"pytest -q": 1}
    assert "retry budget (0)" in execution.summary


def test_run_cli_runtime_loop_stops_at_the_iteration_budget() -> None:
    """Read-only loops without progress should stop with a typed no-progress reason."""
    adapter = _ScriptedAdapter(
        [CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd")]
    )
    session = _FakeSession({"pwd": _command_result("pwd", output="/workspace/repo\n")})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=1, worker_timeout_seconds=30),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "no_progress_before_budget"
    assert "without meaningful task progress" in execution.summary
    assert len(execution.commands_run) == 1


def test_run_cli_runtime_loop_stops_at_max_iterations_after_write_progress() -> None:
    """Max-iteration remains the stop reason once concrete write progress has started."""
    adapter = _ScriptedAdapter(
        [CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="touch note.txt")]
    )
    session = _FakeSession({"touch note.txt": _command_result("touch note.txt", output="")})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=1, worker_timeout_seconds=30),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "max_iterations"
    assert "max iteration budget (1)" in execution.summary


def test_run_cli_runtime_loop_stops_as_stalled_in_inspection_after_write_progress() -> None:
    """Repeated read-only loops after an initial write should emit stalled_in_inspection."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="touch a.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat a.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat a.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat a.txt"),
        ]
    )
    session = _FakeSession(
        {
            "touch a.txt": _command_result("touch a.txt", output=""),
            "cat a.txt": _command_result("cat a.txt", output="x\n"),
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=6,
            worker_timeout_seconds=30,
            stall_window_iterations=2,
            max_repeated_file_reads=2,
            stall_correction_turns=0,
        ),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "stalled_in_inspection"
    assert "stalled in repeated inspection" in execution.summary


def test_run_cli_runtime_loop_resets_repeated_read_tracking_after_write() -> None:
    """A concrete write should reset repeated-read counters before post-write inspection."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat a.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat b.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat a.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="touch a.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat a.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat c.txt"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat a.txt"),
            CliRuntimeStep(kind="final", final_output="done"),
        ]
    )
    session = _FakeSession(
        {
            "cat a.txt": _command_result("cat a.txt", output="x\n"),
            "cat b.txt": _command_result("cat b.txt", output="y\n"),
            "touch a.txt": _command_result("touch a.txt", output=""),
            "cat c.txt": _command_result("cat c.txt", output="z\n"),
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=8,
            worker_timeout_seconds=30,
            stall_window_iterations=3,
            max_repeated_file_reads=2,
            stall_correction_turns=0,
        ),
    )

    assert execution.status == "success"
    assert execution.stop_reason == "final_answer"
    assert execution.summary == "done"


def test_run_cli_runtime_loop_stops_as_exploration_exhausted() -> None:
    """Exploration-phase budget should stop broad read-only probing before max iterations."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="ls"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat README.md"),
        ]
    )
    session = _FakeSession(
        {
            "ls": _command_result("ls", output="README.md\n"),
            "pwd": _command_result("pwd", output="/workspace/repo\n"),
            "cat README.md": _command_result("cat README.md", output="hello\n"),
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=6,
            worker_timeout_seconds=30,
            max_exploration_iterations=1,
            stall_correction_turns=0,
        ),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "exploration_exhausted"
    assert "exploration-phase budget" in execution.summary


def test_run_cli_runtime_loop_allows_correction_turn_adapter_response() -> None:
    """Correction turns should still dispatch to adapter so it can respond to guidance."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="ls"),
            CliRuntimeStep(kind="final", final_output="Provided plan after correction."),
        ]
    )
    session = _FakeSession({"ls": _command_result("ls", output="README.md\n")})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=3,
            worker_timeout_seconds=30,
            max_exploration_iterations=1,
            stall_correction_turns=1,
        ),
    )

    assert execution.status == "success"
    assert execution.stop_reason == "final_answer"
    assert execution.summary == "Provided plan after correction."
    assert len(adapter.calls) == 2
    assert any(
        message.role == "assistant"
        and "Runtime corrective message: exploration budget is nearly exhausted." in message.content
        for message in adapter.calls[1]
    )


def test_run_cli_runtime_loop_deduplicates_file_hints_per_turn_for_read_counts() -> None:
    """Repeated file tokens within one command should only count once for stall counters."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_bash",
                tool_input="cat file.txt file.txt file.txt",
            ),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="cat other.txt"),
            CliRuntimeStep(kind="final", final_output="done"),
        ]
    )
    session = _FakeSession(
        {
            "cat file.txt file.txt file.txt": _command_result(
                "cat file.txt file.txt file.txt",
                output="x\nx\nx\n",
            ),
            "cat other.txt": _command_result("cat other.txt", output="y\n"),
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=4,
            worker_timeout_seconds=30,
            stall_window_iterations=2,
            max_repeated_file_reads=2,
            stall_correction_turns=0,
        ),
    )

    assert execution.status == "success"
    assert execution.stop_reason == "final_answer"
    assert execution.summary == "done"


def test_run_cli_runtime_loop_stops_at_the_worker_timeout() -> None:
    """The runtime should return a structured failure when the overall timeout is exhausted."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(kind="final", final_output="late answer"),
        ]
    )
    session = _FakeSession({"pwd": _command_result("pwd", output="/workspace/repo\n")})
    clock_values = iter([0.0, 0.0, 0.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0])

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=1),
        clock=lambda: next(clock_values),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "worker_timeout"
    assert "worker timeout (1s)" in execution.summary


def test_run_cli_runtime_loop_logs_warning_near_context_window_limit(caplog) -> None:
    """Prompt-size preflight should warn once usage crosses 80% of the model limit."""
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")])
    session = _FakeSession({})

    with caplog.at_level("WARNING"):
        execution = run_cli_runtime_loop(
            adapter,
            session,
            system_prompt=("x" * 33),
            settings=CliRuntimeSettings(
                max_iterations=1,
                worker_timeout_seconds=30,
                context_window_limit_tokens=12,
            ),
        )

    assert execution.status == "success"
    assert "context-window warning threshold" in caplog.text


def test_run_cli_runtime_loop_fails_fast_when_prompt_exceeds_context_window() -> None:
    """Oversized prompts should stop before adapter dispatch with a typed context-window reason."""
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")])
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt=("x" * 220),
        settings=CliRuntimeSettings(
            max_iterations=1,
            worker_timeout_seconds=30,
            context_window_limit_tokens=20,
        ),
    )

    assert execution.status == "failure"
    assert execution.stop_reason == "context_window"
    assert "context window" in execution.summary.lower()
    assert adapter.calls == []


def test_run_cli_runtime_loop_returns_shell_errors_without_raising() -> None:
    """Shell-session failures should become structured runtime errors."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_bash",
                tool_input="pytest -q",
            )
        ]
    )
    session = _FakeSession({"pytest -q": DockerShellSessionError("shell exploded")})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(),
    )

    assert execution.status == "error"
    assert execution.stop_reason == "shell_error"
    assert "shell exploded" in execution.summary


def test_run_cli_runtime_loop_returns_adapter_error_for_unknown_tools() -> None:
    """Unknown tool requests should surface as structured adapter errors."""
    adapter = _ScriptedAdapter(
        [CliRuntimeStep(kind="tool_call", tool_name="missing_tool", tool_input="pwd")]
    )
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(),
    )

    assert execution.status == "error"
    assert execution.stop_reason == "adapter_error"
    assert "unknown tool" in execution.summary.lower()


def test_run_cli_runtime_loop_recovers_from_plan_mode_unknown_tool() -> None:
    """Gemini plan-mode control tools should be recoverable unknown-tool observations."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="enter_plan_mode", tool_input="{}"),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(kind="final", final_output="done"),
        ]
    )
    session = _FakeSession({"pwd": _command_result("pwd", output="/workspace/repo\n")})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=4, worker_timeout_seconds=30),
    )

    assert execution.status == "success"
    assert execution.summary == "done"
    assert [command.command for command in execution.commands_run] == ["pwd"]
    assert any(
        message.role == "tool"
        and message.tool_name == "enter_plan_mode"
        and "unavailable_tool" in message.content
        for message in execution.messages
    )


def test_normalize_requested_tool_name_maps_exec_command_aliases() -> None:
    """Common external adapter names should map onto registered execute_bash."""
    assert _normalize_requested_tool_name("functions.exec_command") == "execute_bash"
    assert _normalize_requested_tool_name("exec_command") == "execute_bash"
    assert _normalize_requested_tool_name("bash") == "execute_bash"
    assert _normalize_requested_tool_name("execute_git") == "execute_git"
