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
    _coerce_non_negative_int,
    _estimate_messages_characters,
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
            "max_observation_characters": 512,
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
    assert settings.max_observation_characters == 512


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


def test_run_cli_runtime_loop_rejects_invalid_str_replace_editor_input() -> None:
    """Structured editor requests should fail clearly on invalid tool input."""
    adapter = _ScriptedAdapter(
        [
            CliRuntimeStep(
                kind="tool_call",
                tool_name="str_replace_editor",
                tool_input='{"path":"README.md","old_text":"", "new_text":"replacement"}',
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
    assert "invalid input for `str_replace_editor`" in execution.summary
    assert session.calls == []


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
    """The runtime should fail cleanly when no final answer appears in time."""
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
    assert execution.stop_reason == "max_iterations"
    assert "max iteration budget (1)" in execution.summary
    assert len(execution.commands_run) == 1


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
