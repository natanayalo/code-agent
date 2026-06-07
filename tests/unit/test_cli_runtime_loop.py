# ruff: noqa: F403, F405
"""Behavior-focused CLI runtime tests."""

from __future__ import annotations

from tests.unit.cli_runtime_support import *  # noqa: F403


def test_run_cli_runtime_loop_uses_tool_client_timeout_and_metadata() -> None:
    """Tool client metadata should drive the transcript and command timeout."""
    execute_bash_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash").model_copy(
        update={"timeout_seconds": 3}
    )
    tool_registry = ToolRegistry(tools=(execute_bash_tool,))
    tool_client = McpToolClient.from_registry(tool_registry)
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(kind="final", final_output="Checked the working directory."),
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
            command_timeout_seconds=9,
        ),
        tool_client=tool_client,
    )

    assert execution.status == "success"
    assert session.calls == [("pwd", 3)]
    assert "Required permission: workspace_write" in execution.messages[1].content
    assert "Default timeout seconds: 3" in execution.messages[1].content
    assert "Expected artifacts: stdout, stderr, changed_files" in execution.messages[1].content
    assert execution.budget_ledger == CliRuntimeBudgetLedger(
        max_iterations=2,
        max_tool_calls=None,
        max_shell_commands=None,
        max_retries=None,
        max_verifier_passes=None,
        iterations_used=2,
        tool_calls_used=1,
        shell_commands_used=1,
        retries_used=0,
        verifier_passes_used=0,
        failed_command_attempts={},
        wall_clock_seconds=execution.budget_ledger.wall_clock_seconds,
    )


def test_run_cli_runtime_loop_handles_noop_span_context(monkeypatch) -> None:
    """Runtime loop should remain functional when span contexts are no-ops."""
    monkeypatch.setattr(
        "workers.cli_runtime.start_optional_span",
        lambda **_kwargs: nullcontext(),
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(kind="final", final_output="Done."),
        ]
    )
    session = _FakeSession({"pwd": _command_result("pwd", output="/workspace/repo\n")})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
    )

    assert execution.status == "success"
    assert execution.stop_reason == "final_answer"
    assert [command.command for command in execution.commands_run] == ["pwd"]


def test_run_cli_runtime_loop_completes_a_multi_turn_sequence() -> None:
    """The runtime should alternate tool calls and observations until a final answer arrives."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_bash",
                tool_input="printf 'done\\n' > note.txt",
            ),
            CliRuntimeStep(kind="final", final_output="Created note.txt and verified the result."),
        ]
    )
    session = _FakeSession(
        {
            "pwd": _command_result("pwd", output="/workspace/repo\n"),
            "printf 'done\\n' > note.txt": _command_result(
                "printf 'done\\n' > note.txt",
                output="",
            ),
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=4, worker_timeout_seconds=30),
    )

    assert execution.status == "success"
    assert execution.stop_reason == "final_answer"
    assert execution.summary == "Created note.txt and verified the result."
    assert [command.command for command in execution.commands_run] == [
        "pwd",
        "printf 'done\\n' > note.txt",
    ]
    assert any(message.role == "tool" for message in execution.messages)
    assert "Tool call: execute_bash" in execution.messages[1].content
    assert "Exit code: 0" in execution.messages[2].content


def test_run_cli_runtime_loop_rejects_tool_call_payload_returned_as_final_output() -> None:
    """Malformed adapter finals that contain tool_call JSON should fail as adapter errors."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="final",
                final_output=(
                    '{"kind":"tool_call","tool_name":"execute_bash",'
                    '"tool_input":"cat > docs/architecture.md <<EOF\\nupdated\\nEOF",'
                    '"final_output":null}'
                ),
            )
        ]
    )
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
    )

    assert execution.status == "error"
    assert execution.stop_reason == "adapter_error"
    assert "tool_call payload as final output" in (execution.summary or "")
    assert execution.commands_run == []


def test_run_cli_runtime_loop_rejects_malformed_tool_call_like_final_output() -> None:
    """Heuristic guard should reject tool_call-like final text even when JSON is malformed."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="final",
                final_output=(
                    '{"kind":"tool_call","tool_name":"execute_bash",'
                    '"tool_input":"echo \\`bad-json-escape\\`","final_output":null}'
                ),
            )
        ]
    )
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
    )

    assert execution.status == "error"
    assert execution.stop_reason == "adapter_error"
    assert "tool_call payload as final output" in (execution.summary or "")
    assert execution.commands_run == []


def test_run_cli_runtime_loop_allows_valid_final_step_json_with_tool_call_keywords() -> None:
    """Heuristic guard should not fire when a valid parsed final-step payload is returned."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="final",
                final_output=(
                    '{"kind":"final","final_output":"summary includes tool_name and '
                    'tool_input as plain text for documentation"}'
                ),
            )
        ]
    )
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
    )

    assert execution.status == "success"
    assert execution.stop_reason == "final_answer"
    assert execution.commands_run == []


def test_run_cli_runtime_loop_skips_condensation_when_history_is_within_threshold() -> None:
    """Short histories should be passed to the adapter unchanged."""
    adapter = _ScriptedAdapter([CliRuntimeStep(kind="final", final_output="done")])
    session = _FakeSession({})

    run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=1,
            worker_timeout_seconds=30,
            context_condenser_threshold_characters=4096,
            context_condenser_recent_messages=2,
        ),
    )

    assert len(adapter.calls) == 1
    assert [message.role for message in adapter.calls[0]] == ["system"]
    assert "Condensed context summary" not in adapter.calls[0][0].content


def test_run_cli_runtime_loop_condenses_older_messages_for_long_histories() -> None:
    """Long transcripts should condense older turns while keeping recent raw detail."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_bash",
                tool_input="printf 'hello' > src/app.py",
            ),
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_bash",
                tool_input="pytest -q",
            ),
            CliRuntimeStep(kind="final", final_output="completed"),
        ]
    )
    session = _FakeSession(
        {
            "printf 'hello' > src/app.py": _command_result(
                "printf 'hello' > src/app.py",
                output="created\n" + ("A" * 1500),
            ),
            "pytest -q": _command_result(
                "pytest -q",
                output="F tests/test_app.py::test_flow\n",
                exit_code=1,
            ),
        }
    )

    run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=4,
            worker_timeout_seconds=30,
            context_condenser_threshold_characters=1200,
            context_condenser_recent_messages=2,
            context_condenser_summary_max_characters=420,
            max_observation_characters=1600,
        ),
    )

    assert len(adapter.calls) == 3
    condensed_call = adapter.calls[2]
    assert condensed_call[1].role == "assistant"
    assert "Condensed context summary" in condensed_call[1].content
    assert "Key decisions made" in condensed_call[1].content
    assert "src/app.py" in condensed_call[1].content
    assert (
        "last command `printf 'hello' > src/app.py` exited with code 0" in condensed_call[1].content
    )
    assert any(
        message.role == "tool" and "Command: pytest -q" in message.content
        for message in condensed_call
    )


def test_run_cli_runtime_loop_keeps_condensed_prompt_within_threshold_when_possible() -> None:
    """Condensed adapter messages should stay within the configured threshold."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call", tool_name="execute_bash", tool_input="touch src/new.py"
            ),
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="ls src"),
            CliRuntimeStep(kind="final", final_output="done"),
        ]
    )
    session = _FakeSession(
        {
            "touch src/new.py": _command_result(
                "touch src/new.py",
                output=("x" * 1500),
            ),
            "ls src": _command_result(
                "ls src",
                output="new.py\n",
            ),
        }
    )
    threshold = 1200

    run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(
            max_iterations=4,
            worker_timeout_seconds=30,
            context_condenser_threshold_characters=threshold,
            context_condenser_recent_messages=2,
            context_condenser_summary_max_characters=300,
            max_observation_characters=1700,
        ),
    )

    assert len(adapter.calls) == 3
    assert _estimate_messages_characters(adapter.calls[2]) <= threshold
