"""Unit tests for the shared CLI worker runtime."""

from __future__ import annotations

import subprocess
from pathlib import Path

from sandbox import DockerShellCommandResult, DockerShellSessionError
from tools import (
    DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS,
    DEFAULT_TOOL_REGISTRY,
    McpToolClient,
    ToolPermissionLevel,
    ToolRegistry,
    build_str_replace_editor_command_from_input,
    build_view_file_command_from_input,
)
from workers.cli_runtime import (
    CliRuntimeBudgetLedger,
    CliRuntimeMessage,
    CliRuntimeSettings,
    CliRuntimeStep,
    _build_condensed_context_summary,
    _coerce_non_negative_int,
    _estimate_messages_characters,
    _extract_file_hints_from_command,
    _looks_read_only_command,
    _messages_for_adapter_turn,
    _normalize_requested_tool_name,
    collect_changed_files,
    collect_changed_files_from_repo_path,
    format_bash_observation,
    run_cli_runtime_loop,
    settings_from_budget,
)


class _ScriptedAdapter:
    def __init__(self, steps: list[CliRuntimeStep]) -> None:
        self._steps = list(steps)
        self.calls: list[list[CliRuntimeMessage]] = []

    def next_step(
        self,
        messages: list[CliRuntimeMessage],
        *,
        system_prompt: str | None = None,
        prompt_override: str | None = None,
        working_directory: Path | None = None,
    ) -> CliRuntimeStep:
        self.calls.append(list(messages))
        if not self._steps:
            raise AssertionError("Adapter received more turns than expected.")
        return self._steps.pop(0)


class _FakeSession:
    def __init__(self, responses: dict[str, DockerShellCommandResult | Exception]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        self.calls.append((command, timeout_seconds))
        response = self._responses[command]
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _command_result(command: str, *, output: str, exit_code: int = 0) -> DockerShellCommandResult:
    return DockerShellCommandResult(
        command=command,
        output=output,
        exit_code=exit_code,
        duration_seconds=0.25,
    )


def test_settings_from_budget_applies_supported_runtime_overrides() -> None:
    """Budget fields should override the inner-loop defaults we support today."""
    settings = settings_from_budget(
        {
            "max_iterations": "12",
            "max_minutes": 2,
            "command_timeout_seconds": 9,
            "max_tool_calls": "5",
            "max_shell_commands": 6,
            "max_retries": 0,
            "max_verifier_passes": "1",
            "max_exploration_iterations": 4,
            "max_execution_iterations": "6",
            "stall_window_iterations": 5,
            "max_repeated_file_reads": "7",
            "stall_correction_turns": 2,
            "max_observation_characters": 512,
            "context_window_limit_tokens": "64000",
        },
        defaults=CliRuntimeSettings(max_iterations=4, worker_timeout_seconds=30),
    )

    assert settings.max_iterations == 12
    assert settings.worker_timeout_seconds == 120
    assert settings.command_timeout_seconds == 9
    assert settings.max_tool_calls == 5
    assert settings.max_shell_commands == 6
    assert settings.max_retries == 0
    assert settings.max_verifier_passes == 1
    assert settings.max_exploration_iterations == 4
    assert settings.max_execution_iterations == 6
    assert settings.stall_window_iterations == 5
    assert settings.max_repeated_file_reads == 7
    assert settings.stall_correction_turns == 2
    assert settings.max_observation_characters == 512
    assert settings.context_window_limit_tokens == 64000


def test_settings_from_budget_accepts_fractional_numeric_strings_like_float_inputs() -> None:
    """Numeric strings should be coerced with the same truncation behavior as float inputs."""
    settings = settings_from_budget(
        {
            "max_iterations": "2.5",
            "command_timeout_seconds": "9.9",
        },
        defaults=CliRuntimeSettings(max_iterations=4, command_timeout_seconds=30),
    )

    assert settings.max_iterations == 2
    assert settings.command_timeout_seconds == 9


def test_settings_from_budget_accepts_zero_for_tool_and_shell_limits() -> None:
    """Zero should be a valid explicit limit for tool/shell execution budgets."""
    settings = settings_from_budget(
        {
            "max_tool_calls": 0,
            "max_shell_commands": "0",
        },
        defaults=CliRuntimeSettings(max_tool_calls=None, max_shell_commands=None),
    )

    assert settings.max_tool_calls == 0
    assert settings.max_shell_commands == 0


def test_coerce_non_negative_int_rejects_non_finite_floats() -> None:
    """NaN and infinity should be ignored instead of crashing runtime budget parsing."""
    assert _coerce_non_negative_int(float("nan")) is None
    assert _coerce_non_negative_int(float("inf")) is None
    assert _coerce_non_negative_int(float("-inf")) is None


def test_format_bash_observation_truncates_long_output() -> None:
    """Shell observations should stay bounded and call out truncation explicitly."""
    observation = format_bash_observation(
        _command_result("cat README.md", output="abcdefghij"),
        max_characters=5,
    )

    assert "Command: cat README.md" in observation
    assert "Exit code: 0" in observation
    assert "abcde" in observation
    assert "[output truncated to 5 characters]" in observation


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


def test_extract_file_hints_includes_extensionless_root_file_arguments() -> None:
    """Heuristics should include common extensionless root-path file arguments."""
    assert "main.py" in _extract_file_hints_from_command("awk '{print $1}' main.py")
    assert "VERSION" in _extract_file_hints_from_command("cat VERSION")
    assert "LICENSE" in _extract_file_hints_from_command("rm LICENSE")
    assert "install" not in _extract_file_hints_from_command("pip install pytest")
    assert "LICENSE" in _extract_file_hints_from_command("grep TODO LICENSE")
    assert "TODO" not in _extract_file_hints_from_command("grep TODO LICENSE")
    assert "manage.py" in _extract_file_hints_from_command("python manage.py")
    assert "build" in _extract_file_hints_from_command("mkdir build")
    assert "build" in _extract_file_hints_from_command("rmdir build")
    assert "LICENSE" in _extract_file_hints_from_command("chmod 644 LICENSE")
    assert "root" not in _extract_file_hints_from_command("chown root LICENSE")
    assert "LICENSE" in _extract_file_hints_from_command("chown root LICENSE")
    assert "s/foo/bar/" not in _extract_file_hints_from_command("sed s/foo/bar/ LICENSE")
    assert "LICENSE" in _extract_file_hints_from_command("sed s/foo/bar/ LICENSE")
    assert "LICENSE" in _extract_file_hints_from_command("git add LICENSE")
    assert "status" not in _extract_file_hints_from_command("git status")
    assert "main" not in _extract_file_hints_from_command("git checkout main")


def test_extract_file_hints_handles_compound_shell_commands() -> None:
    """Heuristics should reset command context across shell separators."""
    hints = _extract_file_hints_from_command(
        "cat file1 && ls file2 | grep needle file3 |& cat file4 & cat file5"
    )

    assert "file1" in hints
    assert "file2" in hints
    assert "file3" in hints
    assert "file4" in hints
    assert "file5" in hints
    assert "ls" not in hints
    assert "grep" not in hints
    assert "needle" not in hints


def test_extract_file_hints_skips_redirection_tokens() -> None:
    """Shell redirection operators should not be classified as file hints."""
    hints = _extract_file_hints_from_command(
        "cat input.txt < src.txt > out.txt 2>&1 1> one.txt 1>> one-append.txt "
        "&> both.txt &>> both-append.txt >| force.txt |& grep failure"
    )

    assert "input.txt" in hints
    assert "src.txt" in hints
    assert "out.txt" in hints
    assert "one.txt" in hints
    assert "one-append.txt" in hints
    assert "both.txt" in hints
    assert "both-append.txt" in hints
    assert "force.txt" in hints
    assert "<" not in hints
    assert "2>&1" not in hints
    assert "1>" not in hints
    assert "1>>" not in hints
    assert "&>" not in hints
    assert "&>>" not in hints
    assert ">|" not in hints
    assert "|&" not in hints


def test_extract_file_hints_skips_current_and_parent_directory_tokens() -> None:
    """Directory shorthand tokens should not be treated as touched file hints."""
    hints = _extract_file_hints_from_command("ls . && cat ../notes.txt && cat ..")

    assert "../notes.txt" in hints
    assert "." not in hints
    assert ".." not in hints


def test_looks_read_only_command_uses_word_boundary_for_short_commands() -> None:
    """Short read-only commands should not match unrelated command-name prefixes."""
    assert _looks_read_only_command("ls") is True
    assert _looks_read_only_command("pwd") is True
    assert _looks_read_only_command("ls src") is True
    assert _looks_read_only_command("pwd /tmp") is True
    assert _looks_read_only_command("lsrc") is False
    assert _looks_read_only_command("pwd_helper") is False


def test_build_condensed_context_summary_truncation_stays_within_budget() -> None:
    """Truncation notice should fit inside the configured summary character budget."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "printf 'hello world' > src/long_name.py\n```"
            ),
        ),
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: printf 'hello world' > src/long_name.py\n"
                "Exit code: 0\nDuration seconds: 0.250\nOutput:\n```text\n" + ("x" * 300) + "\n```"
            ),
        ),
    ]
    max_characters = 120
    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=max_characters,
    )

    assert len(summary) <= max_characters
    assert summary.endswith("characters]")


def test_build_condensed_context_summary_prefers_most_recent_file_hints() -> None:
    """File hints should keep chronological order and show the most recent entries."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch f1 f2 f3 f4 f5 f6 f7 f8 f9 f10\n```"
            ),
        ),
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: touch f1 f2 f3 f4 f5 f6 f7 f8 f9 f10\n"
                "Exit code: 0\nDuration seconds: 0.250\nOutput:\n```text\nok\n```"
            ),
        ),
    ]
    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=5000,
    )

    assert "- Files touched hints: `f3`, `f4`, `f5`, `f6`, `f7`, `f8`, `f9`, `f10`" in summary
    assert "`f1`" not in summary


def test_build_condensed_context_summary_escapes_backticks_in_inline_code() -> None:
    """Commands containing backticks should remain valid inline markdown/code text."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "echo `date` > out.txt\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "``echo `date` > out.txt``" in summary


def test_build_condensed_context_summary_escapes_edge_backticks_in_inline_code() -> None:
    """Inline-code rendering should stay valid when text starts/ends with backticks."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "`date`\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "`` `date` ``" in summary


def test_build_condensed_context_summary_escapes_backticks_in_current_state() -> None:
    """Current-state command formatting should remain valid when commands contain backticks."""
    older_messages = [
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: echo `date` > out.txt\n"
                "Exit code: 0\nDuration seconds: 0.250\nOutput:\n```text\nok\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "last command ``echo `date` > out.txt`` exited with code 0" in summary


def test_build_condensed_context_summary_parses_bash_fence_with_trailing_tag_spaces() -> None:
    """Command extraction should tolerate optional spaces after the bash language tag."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash   \n"
                "touch spaced_tag.txt\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "`touch spaced_tag.txt`" in summary


def test_build_condensed_context_summary_parses_text_fence_with_trailing_tag_spaces() -> None:
    """Output excerpt extraction should tolerate optional spaces after the text tag."""
    older_messages = [
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: pytest -q\n"
                "Exit code: 1\nDuration seconds: 0.250\nOutput:\n```text \n"
                "F tests/test_flow.py::test_case\n```\n"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "exit 1 (F tests/test_flow.py::test_case)" in summary


def test_build_condensed_context_summary_uses_last_non_empty_output_line() -> None:
    """Error excerpt should prefer the final non-empty output line."""
    older_messages = [
        CliRuntimeMessage(
            role="tool",
            tool_name="execute_bash",
            content=(
                "Tool result: execute_bash\nCommand: pytest -q\n"
                "Exit code: 1\nDuration seconds: 0.250\nOutput:\n```text\n"
                "header line\n"
                "\n"
                "final failure detail\n"
                "```\n"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=2000,
    )

    assert "exit 1 (final failure detail)" in summary


def test_messages_for_adapter_turn_preserves_history_when_trimming_recent_tail() -> None:
    """Messages dropped from recent tail should be merged into summary, not lost."""
    messages = [
        CliRuntimeMessage(role="system", content="System prompt"),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch very_old.txt\n```\n" + ("w" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch older.txt\n```\n" + ("x" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch moved_to_summary.txt\n```\n" + ("y" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch stays_recent.txt\n```\n" + ("z" * 900)
            ),
        ),
    ]

    condensed = _messages_for_adapter_turn(
        messages,
        settings=CliRuntimeSettings(
            context_condenser_threshold_characters=1200,
            context_condenser_recent_messages=3,
            context_condenser_summary_max_characters=800,
            max_iterations=2,
            worker_timeout_seconds=30,
        ),
    )

    assert condensed[1].role == "assistant"
    assert "moved_to_summary.txt" in condensed[1].content
    assert all("moved_to_summary.txt" not in message.content for message in condensed[2:])
    assert any("touch stays_recent.txt" in message.content for message in condensed[2:])


def test_messages_for_adapter_turn_rebuilds_compact_summary_with_truncation_notice() -> None:
    """Final compact summary should be rebuilt and retain structured truncation metadata."""
    long_name_1 = "very_old_" + ("a" * 220) + ".txt"
    long_name_2 = "older_" + ("b" * 220) + ".txt"
    long_name_3 = "keep_recent_1_" + ("c" * 220) + ".txt"
    long_name_4 = "keep_recent_2_" + ("d" * 220) + ".txt"
    messages = [
        CliRuntimeMessage(role="system", content="System prompt\n" + ("s" * 500)),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                f"touch {long_name_1}\n```\n" + ("w" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                f"touch {long_name_2}\n```\n" + ("x" * 900)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                f"touch {long_name_3}\n```\n" + ("y" * 600)
            ),
        ),
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                f"touch {long_name_4}\n```\n" + ("z" * 600)
            ),
        ),
    ]

    condensed = _messages_for_adapter_turn(
        messages,
        settings=CliRuntimeSettings(
            context_condenser_threshold_characters=1024,
            context_condenser_recent_messages=2,
            context_condenser_summary_max_characters=800,
            max_iterations=2,
            worker_timeout_seconds=30,
        ),
    )

    assert condensed[1].role == "assistant"
    assert "Condensed context summary" in condensed[1].content
    assert condensed[1].content.endswith("characters]")


def test_build_condensed_context_summary_prefers_latest_unique_file_occurrences() -> None:
    """Deduping should preserve latest unique file mentions before applying the tail window."""
    older_messages = [
        CliRuntimeMessage(
            role="assistant",
            content=(
                "Tool call: execute_bash\nRequired permission: workspace_write\n"
                "Default timeout seconds: 30\nExpected artifacts: stdout\n```bash\n"
                "touch f1 f2 f3 f4 f5 f6 f7 f8 f9 f1\n```"
            ),
        )
    ]

    summary = _build_condensed_context_summary(
        older_messages,
        max_characters=5000,
    )

    assert "- Files touched hints: `f3`, `f4`, `f5`, `f6`, `f7`, `f8`, `f9`, `f1`" in summary


def test_run_cli_runtime_loop_executes_git_helper_requests() -> None:
    """Git helper tool calls should be normalized into safe git shell commands."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_git",
                tool_input='{"operation":"status","porcelain":true}',
            ),
            CliRuntimeStep(kind="final", final_output="Collected git status."),
        ]
    )
    session = _FakeSession(
        {
            "git status --porcelain=v1 --untracked-files=all": _command_result(
                "git status --porcelain=v1 --untracked-files=all",
                output=" M tools/registry.py\n",
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == "git status --porcelain=v1 --untracked-files=all"
    assert 1 <= session.calls[0][1] <= 30
    assert "Tool call: execute_git" in execution.messages[1].content
    assert "Tool result: execute_git" in execution.messages[2].content


def test_run_cli_runtime_loop_executes_github_helper_requests() -> None:
    """GitHub helper tool calls should be normalized into safe gh shell commands."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_github",
                tool_input=(
                    '{"operation":"pr_comment","repository_full_name":"openai/code-agent",'
                    '"pr_number":59,"comment_body":"Looks good."}'
                ),
            ),
            CliRuntimeStep(kind="final", final_output="Posted PR comment."),
        ]
    )
    session = _FakeSession(
        {
            "gh pr comment 59 --repo=openai/code-agent '--body=Looks good.'": _command_result(
                "gh pr comment 59 --repo=openai/code-agent '--body=Looks good.'",
                output="comment-url\n",
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.NETWORKED_WRITE,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == "gh pr comment 59 --repo=openai/code-agent '--body=Looks good.'"
    assert 1 <= session.calls[0][1] <= 30
    assert "Tool call: execute_github" in execution.messages[1].content
    assert "Tool result: execute_github" in execution.messages[2].content


def test_run_cli_runtime_loop_executes_browser_helper_requests() -> None:
    """Browser helper tool calls should be normalized into safe curl commands."""
    expected_browser_command = (
        "curl --fail --silent --show-error --location "
        f"--max-time={DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS} --globoff --get "
        "--url=https://en.wikipedia.org/w/api.php --data-urlencode=action=opensearch "
        "--data-urlencode=search=langgraph --data-urlencode=limit=3 "
        "--data-urlencode=namespace=0 --data-urlencode=format=json"
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_browser",
                tool_input='{"operation":"search","query":"langgraph","limit":3}',
            ),
            CliRuntimeStep(kind="final", final_output="Collected search results."),
        ]
    )
    session = _FakeSession(
        {
            expected_browser_command: _command_result(
                expected_browser_command,
                output='["langgraph", ["LangGraph"], ["A framework"], ["https://example.com"]]\n',
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.NETWORKED_WRITE,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == expected_browser_command
    assert 1 <= session.calls[0][1] <= 30
    assert "Tool call: execute_browser" in execution.messages[1].content
    assert "Tool result: execute_browser" in execution.messages[2].content


def test_run_cli_runtime_loop_executes_view_file_tool_requests() -> None:
    """View-file tool calls should be normalized into bounded line-number output commands."""
    tool_input = '{"path":"README.md","start_line":2,"end_line":4}'
    expected_command = build_view_file_command_from_input(tool_input)
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="view_file", tool_input=tool_input),
            CliRuntimeStep(kind="final", final_output="Viewed README segment."),
        ]
    )
    session = _FakeSession(
        {
            expected_command: _command_result(
                expected_command,
                output="     2  line two\n     3  line three\n     4  line four\n",
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == expected_command
    assert "Tool call: view_file" in execution.messages[1].content
    assert "Tool result: view_file" in execution.messages[2].content


def test_run_cli_runtime_loop_executes_str_replace_editor_tool_requests() -> None:
    """str_replace_editor calls should route through the structured replacement wrapper."""
    tool_input = '{"path":"README.md","old_text":"hello","new_text":"hello world"}'
    expected_command = build_str_replace_editor_command_from_input(tool_input)
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="str_replace_editor",
                tool_input=tool_input,
            ),
            CliRuntimeStep(kind="final", final_output="Updated README greeting."),
        ]
    )
    session = _FakeSession(
        {
            expected_command: _command_result(
                expected_command,
                output="",
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert execution.status == "success"
    assert len(session.calls) == 1
    assert session.calls[0][0] == expected_command
    assert "Tool call: str_replace_editor" in execution.messages[1].content
    assert "Tool result: str_replace_editor" in execution.messages[2].content


def test_run_cli_runtime_loop_uses_browser_tool_timeout_for_command_rendering() -> None:
    """Browser command generation should use the timeout configured on the tool definition."""
    browser_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_browser").model_copy(
        update={"timeout_seconds": 7}
    )
    tool_client = McpToolClient.from_registry(ToolRegistry(tools=(browser_tool,)))
    expected_browser_command = (
        "curl --fail --silent --show-error --location "
        "--max-time=7 --globoff --get "
        "--url=https://en.wikipedia.org/w/api.php --data-urlencode=action=opensearch "
        "--data-urlencode=search=langgraph --data-urlencode=limit=3 "
        "--data-urlencode=namespace=0 --data-urlencode=format=json"
    )
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_browser",
                tool_input='{"operation":"search","query":"langgraph","limit":3}',
            ),
            CliRuntimeStep(kind="final", final_output="Collected search results."),
        ]
    )
    session = _FakeSession(
        {
            expected_browser_command: _command_result(
                expected_browser_command,
                output='["langgraph", ["LangGraph"], ["A framework"], ["https://example.com"]]\n',
            )
        }
    )

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=2, worker_timeout_seconds=30),
        tool_client=tool_client,
        granted_permission=ToolPermissionLevel.NETWORKED_WRITE,
    )

    assert execution.status == "success"
    assert session.calls == [(expected_browser_command, 7)]


def test_run_cli_runtime_loop_rejects_invalid_git_helper_input() -> None:
    """Structured git helper requests should fail clearly on invalid tool input."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="execute_git",
                tool_input='{"operation":"status","message":"bad"}',
            )
        ]
    )
    session = _FakeSession({})

    execution = run_cli_runtime_loop(
        adapter,
        session,
        system_prompt="System prompt",
        settings=CliRuntimeSettings(max_iterations=1, worker_timeout_seconds=30),
    )

    assert execution.status == "error"
    assert execution.stop_reason == "adapter_error"
    assert "invalid input for `execute_git`" in execution.summary
    assert session.calls == []


def test_run_cli_runtime_loop_recovers_from_invalid_str_replace_editor_input() -> None:
    """Invalid str_replace_editor payloads should return feedback and let the adapter recover."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="str_replace_editor",
                tool_input='{"path":"README.md","old_text":"", "new_text":"replacement"}',
            ),
            CliRuntimeStep(kind="final", final_output="Switched to a safer edit approach."),
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
    assert execution.summary == "Switched to a safer edit approach."
    assert session.calls == []
    assert execution.budget_ledger.tool_calls_used == 1
    assert any(
        message.role == "tool"
        and message.tool_name == "str_replace_editor"
        and "input_validation_failed" in message.content
        and "Guidance: for multiline edits, use `execute_bash`" in message.content
        for message in execution.messages
    )


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


def test_run_cli_runtime_loop_stops_at_the_worker_timeout() -> None:
    """The runtime should return a structured failure when the overall timeout is exhausted."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(kind="tool_call", tool_name="execute_bash", tool_input="pwd"),
            CliRuntimeStep(kind="final", final_output="late answer"),
        ]
    )
    session = _FakeSession({"pwd": _command_result("pwd", output="/workspace/repo\n")})
    clock_values = iter([0.0, 0.0, 0.0, 0.0, 0.0, 2.0, 2.0])

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


def test_collect_changed_files_parses_modified_renamed_and_untracked_paths() -> None:
    """Changed file collection should normalize the common porcelain shapes we rely on."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output=" M README.md\0R  new.py\0old.py\0?? tests/test_new.py\0",
            )
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["README.md", "new.py", "tests/test_new.py"]


def test_collect_changed_files_does_not_treat_non_rename_arrow_paths_as_renames() -> None:
    """NUL-delimited porcelain output should preserve literal arrows in ordinary paths."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output=" M docs/name -> value.txt\0?? tests/path -> sample.txt\0",
            )
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["docs/name -> value.txt", "tests/path -> sample.txt"]


def test_collect_changed_files_handles_rename_paths_and_newlines_with_porcelain_z() -> None:
    """Porcelain -z output should preserve literal rename and newline characters in paths."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output="R  new -> name.txt\0old -> name.txt\0?? line\nbreak.txt\0",
            )
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["new -> name.txt", "line\nbreak.txt"]


def test_collect_changed_files_falls_back_when_porcelain_z_raises() -> None:
    """When porcelain -z execution fails, fallback line parsing should still report files."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": DockerShellSessionError("boom"),
            "git status --porcelain=v1 --untracked-files=all": _command_result(
                "git status --porcelain=v1 --untracked-files=all",
                output="?? hello_runtime.txt\n M README.md\nR  old_name.py -> new_name.py\n",
            ),
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["hello_runtime.txt", "README.md", "new_name.py"]
    assert [command for command, _ in session.calls] == [
        "git status --porcelain=v1 -z --untracked-files=all",
        "git status --porcelain=v1 --untracked-files=all",
    ]


def test_collect_changed_files_falls_back_when_porcelain_z_exits_non_zero() -> None:
    """When porcelain -z returns non-zero, fallback line parsing should be attempted."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output="fatal: unsupported option\n",
                exit_code=129,
            ),
            "git status --porcelain=v1 --untracked-files=all": _command_result(
                "git status --porcelain=v1 --untracked-files=all",
                output="?? note.txt\n",
            ),
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["note.txt"]
    assert [command for command, _ in session.calls] == [
        "git status --porcelain=v1 -z --untracked-files=all",
        "git status --porcelain=v1 --untracked-files=all",
    ]


def test_collect_changed_files_returns_empty_when_workspace_is_not_a_git_repo() -> None:
    """Non-repository workspaces should short-circuit changed-file collection quietly."""
    session = _FakeSession(
        {
            "git status --porcelain=v1 -z --untracked-files=all": _command_result(
                "git status --porcelain=v1 -z --untracked-files=all",
                output="fatal: not a git repository (or any of the parent directories): .git\n",
                exit_code=128,
            ),
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == []
    assert [command for command, _ in session.calls] == [
        "git status --porcelain=v1 -z --untracked-files=all"
    ]


def test_collect_changed_files_runs_git_in_explicit_working_directory() -> None:
    """Changed file collection should target the repo path when provided."""
    repo_path = Path("/workspace/repo")
    status_command = "git -C /workspace/repo status --porcelain=v1 -z --untracked-files=all"
    session = _FakeSession(
        {
            status_command: _command_result(
                status_command,
                output="?? runtime_fix_probe.txt\0",
            )
        }
    )

    changed_files = collect_changed_files(session, working_directory=repo_path)

    assert changed_files == ["runtime_fix_probe.txt"]
    assert [command for command, _ in session.calls] == [status_command]


def test_collect_changed_files_from_repo_path_parses_porcelain_z_output(monkeypatch) -> None:
    """Host-side fallback should parse git porcelain output for changed files."""

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=b" M README.md\0?? runtime_ok_2.txt\0",
            stderr=b"",
        )

    monkeypatch.setattr("workers.cli_runtime.subprocess.run", _fake_run)

    changed_files = collect_changed_files_from_repo_path(Path("/tmp/repo"))

    assert changed_files == ["README.md", "runtime_ok_2.txt"]


def test_collect_changed_files_from_repo_path_logs_timeout_details(monkeypatch) -> None:
    """Host fallback should log timeout details when git status exceeds timeout."""

    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    warning_calls: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr("workers.cli_runtime.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "workers.cli_runtime.logger.warning",
        lambda message, **kwargs: warning_calls.append((message, kwargs)),
    )

    changed_files = collect_changed_files_from_repo_path(Path("/tmp/repo"), timeout_seconds=7)

    assert changed_files == []
    assert warning_calls
    assert "timed out" in warning_calls[0][0].lower()
    assert warning_calls[0][1]["extra"] == {"timeout_seconds": 7}
    assert warning_calls[0][1]["exc_info"] is not None
