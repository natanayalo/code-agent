"""Unit tests for the shared CLI worker runtime."""

from __future__ import annotations

from sandbox import DockerShellCommandResult, DockerShellSessionError
from workers.cli_runtime import (
    CliRuntimeMessage,
    CliRuntimeSettings,
    CliRuntimeStep,
    collect_changed_files,
    format_bash_observation,
    run_cli_runtime_loop,
    settings_from_budget,
)


class _ScriptedAdapter:
    def __init__(self, steps: list[CliRuntimeStep]) -> None:
        self._steps = list(steps)
        self.calls: list[list[CliRuntimeMessage]] = []

    def next_step(self, messages: list[CliRuntimeMessage]) -> CliRuntimeStep:
        self.calls.append(list(messages))
        if not self._steps:
            raise AssertionError("Adapter received more turns than expected.")
        return self._steps.pop(0)


class _FakeSession:
    def __init__(self, responses: dict[str, DockerShellCommandResult | Exception]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, int]] = []

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        self.calls.append((command, timeout_seconds))
        response = self._responses[command]
        if isinstance(response, Exception):
            raise response
        return response


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
            "max_observation_characters": 512,
        },
        defaults=CliRuntimeSettings(max_iterations=4, worker_timeout_seconds=30),
    )

    assert settings.max_iterations == 12
    assert settings.worker_timeout_seconds == 120
    assert settings.command_timeout_seconds == 9
    assert settings.max_observation_characters == 512


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
    clock_values = iter([0.0, 0.0, 0.0, 2.0])

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


def test_collect_changed_files_parses_modified_renamed_and_untracked_paths() -> None:
    """Changed file collection should normalize the common porcelain shapes we rely on."""
    session = _FakeSession(
        {
            "git status --short --untracked-files=all": _command_result(
                "git status --short --untracked-files=all",
                output=" M README.md\nR  old.py -> new.py\n?? tests/test_new.py\n",
            )
        }
    )

    changed_files = collect_changed_files(session)

    assert changed_files == ["README.md", "new.py", "tests/test_new.py"]
