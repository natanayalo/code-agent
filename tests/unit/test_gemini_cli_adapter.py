"""Unit tests for the Gemini CLI runtime adapter."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from workers.cli_runtime import CliRuntimeMessage
from workers.gemini_cli_adapter import (
    GeminiCliRuntimeAdapter,
    _build_adapter_prompt,
    _coerce_positive_int,
    _extract_json,
    _truncate_detail,
)


def test_gemini_adapter_parses_bare_json_tool_call(monkeypatch) -> None:
    """A bare JSON tool-call response should be parsed directly."""
    recorded: dict[str, object] = {}

    def fake_run(
        command: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool = False,
        timeout: int,
        env: dict[str, str] | None,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        recorded["command"] = list(command)
        recorded["input"] = input
        recorded["timeout"] = timeout
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"kind":"tool_call","tool_name":"execute_bash"'
                ',"tool_input":"ls -la","final_output":null}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = GeminiCliRuntimeAdapter(
        executable="/usr/local/bin/gemini",
        model="gemini-2.0-flash",
        request_timeout_seconds=30,
    )
    step = adapter.next_step([CliRuntimeMessage(role="system", content="You are a coding agent.")])

    assert step.kind == "tool_call"
    assert step.tool_name == "execute_bash"
    assert step.tool_input == "ls -la"
    assert recorded["command"] == [
        "/usr/local/bin/gemini",
        "chat",
        "--model",
        "gemini-2.0-flash",
        "-o",
        "json",
        "--accept-raw-output-risk",
        "--raw-output",
    ]
    assert recorded["timeout"] == 30
    assert "## Runtime Transcript" in str(recorded["input"])


def test_gemini_adapter_parses_json_in_markdown_fence(monkeypatch) -> None:
    """A JSON object wrapped in a markdown code fence should still be accepted."""

    def fake_run(command, *, input, text, capture_output, timeout, env, check=False, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "Here is my decision:\n"
                "```json\n"
                '{"kind":"final","final_output":"Done.","tool_name":null,"tool_input":null}\n'
                "```\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = GeminiCliRuntimeAdapter()
    step = adapter.next_step([CliRuntimeMessage(role="system", content="Proceed.")])

    assert step.kind == "final"
    assert step.final_output == "Done."


def test_gemini_adapter_surfaces_cli_failures(monkeypatch) -> None:
    """Non-zero exit codes should raise RuntimeError with stderr details."""

    def fake_run(command, *, input, text, capture_output, timeout, env, check=False, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="API key missing")

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = GeminiCliRuntimeAdapter()
    with pytest.raises(RuntimeError, match="API key missing"):
        adapter.next_step([CliRuntimeMessage(role="system", content="Go.")])


def test_gemini_adapter_raises_on_timeout(monkeypatch) -> None:
    """TimeoutExpired from subprocess should be re-raised as RuntimeError."""

    def fake_run(command, *, input, text, capture_output, timeout, env, check=False, **kwargs):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = GeminiCliRuntimeAdapter(request_timeout_seconds=5)
    with pytest.raises(RuntimeError, match="timed out after 5s"):
        adapter.next_step([CliRuntimeMessage(role="system", content="Go.")])


def test_gemini_adapter_raises_on_os_error(monkeypatch) -> None:
    """OSError (e.g. binary not found) should be re-raised as RuntimeError."""

    def fake_run(command, *, input, text, capture_output, timeout, env, check=False, **kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = GeminiCliRuntimeAdapter(executable="/no/such/gemini")
    with pytest.raises(RuntimeError, match="could not start"):
        adapter.next_step([CliRuntimeMessage(role="system", content="Go.")])


def test_gemini_adapter_falls_back_to_final_step_on_no_json(monkeypatch) -> None:
    """A response with no JSON object should fall back to a 'final' step with raw text."""

    def fake_run(command, *, input, text, capture_output, timeout, env, check=False, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout="I cannot help with that.\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = GeminiCliRuntimeAdapter()
    step = adapter.next_step([CliRuntimeMessage(role="system", content="Go.")])

    assert step.kind == "final"
    assert step.final_output == "I cannot help with that."


def test_gemini_adapter_prompt_override_bypasses_runtime_prompt_shaping(monkeypatch) -> None:
    """Prompt overrides should send raw prompt text and wrap non-step JSON as final."""
    recorded: dict[str, object] = {}

    def fake_run(
        command: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool = False,
        timeout: int,
        env: dict[str, str] | None,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        recorded["input"] = input
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"reviewer_kind":"worker_self_review","summary":"ok",'
                '"confidence":0.8,"outcome":"no_findings","findings":[]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = GeminiCliRuntimeAdapter()
    step = adapter.next_step(
        [],
        prompt_override="Review these edits and return ReviewResult JSON only.",
    )

    assert step.kind == "final"
    assert step.final_output is not None
    assert '"reviewer_kind":"worker_self_review"' in step.final_output
    assert recorded["input"] == "Review these edits and return ReviewResult JSON only."


def test_gemini_adapter_from_env_maps_env_vars() -> None:
    """Environment variables should map into the adapter settings."""
    adapter = GeminiCliRuntimeAdapter.from_env(
        {
            "CODE_AGENT_GEMINI_CLI_BIN": "/opt/bin/gemini",
            "CODE_AGENT_GEMINI_MODEL": "gemini-2.5-pro",
            "CODE_AGENT_GEMINI_TIMEOUT_SECONDS": "60",
            "PATH": "/usr/local/bin:/usr/bin",
            "GEMINI_API_KEY": "gemini-key",
            "UNRELATED_SECRET": "must-not-pass",
        }
    )

    assert adapter.executable == "/opt/bin/gemini"
    assert adapter.model == "gemini-2.5-pro"
    assert adapter.request_timeout_seconds == 60
    assert adapter.env == {
        "PATH": "/usr/local/bin:/usr/bin",
        "GEMINI_API_KEY": "gemini-key",
    }


def test_gemini_adapter_from_env_uses_defaults_for_missing_vars() -> None:
    """When optional env vars are absent the adapter uses defaults."""
    adapter = GeminiCliRuntimeAdapter.from_env({})

    assert adapter.executable == "gemini"
    assert adapter.model is None
    assert adapter.request_timeout_seconds == 120


def test_gemini_adapter_scopes_constructor_default_env(monkeypatch) -> None:
    """Direct construction should still scope subprocess env vars by default."""
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")

    adapter = GeminiCliRuntimeAdapter()

    assert adapter.env["PATH"] == "/usr/local/bin:/usr/bin"
    assert adapter.env["GEMINI_API_KEY"] == "gemini-key"
    assert "UNRELATED_SECRET" not in adapter.env


def test_gemini_adapter_command_omits_model_when_not_configured() -> None:
    """The model flag should be absent when no model is set."""
    adapter = GeminiCliRuntimeAdapter(executable="gemini")
    assert adapter._build_command() == [
        "gemini",
        "chat",
        "-o",
        "json",
        "--accept-raw-output-risk",
        "--raw-output",
    ]


def test_gemini_adapter_command_includes_model_when_configured() -> None:
    """The model flag should appear in the command when a model is set."""
    adapter = GeminiCliRuntimeAdapter(executable="gemini", model="gemini-2.0-flash")
    assert adapter._build_command() == [
        "gemini",
        "chat",
        "--model",
        "gemini-2.0-flash",
        "-o",
        "json",
        "--accept-raw-output-risk",
        "--raw-output",
    ]


class TestExtractJson:
    def test_bare_json_returned_as_is(self) -> None:
        raw = '{"kind":"final","final_output":"ok","tool_name":null,"tool_input":null}'
        assert _extract_json(raw) == raw

    def test_json_in_json_fence(self) -> None:
        raw = (
            '```json\n{"kind":"final","final_output":"ok","tool_name":null,"tool_input":null}\n```'
        )
        result = _extract_json(raw)
        assert result.startswith("{")

    def test_json_in_plain_fence(self) -> None:
        raw = '```\n{"kind":"final","final_output":"ok","tool_name":null,"tool_input":null}\n```'
        result = _extract_json(raw)
        assert result.startswith("{")

    def test_json_embedded_in_prose(self) -> None:
        raw = (
            'Here is the result: {"kind":"final","final_output":"done"'
            ',"tool_name":null,"tool_input":null} End.'
        )
        result = _extract_json(raw)
        assert result.startswith("{")

    def test_raises_when_no_json_present(self) -> None:
        with pytest.raises(RuntimeError, match="No JSON object found"):
            _extract_json("No JSON here at all.")

    def test_nested_json_extracted_correctly(self) -> None:
        raw = (
            '{"kind":"final","metadata":{"x":1},'
            '"tool_name":null,"tool_input":null,"final_output":"ok"}'
        )
        result = _extract_json(raw)
        assert result == raw

    def test_trailing_prose_stripped(self) -> None:
        raw = '{"kind":"final","final_output":"done","tool_name":null,"tool_input":null} Done.'
        result = _extract_json(raw)
        assert result.endswith("}")
        assert "Done." not in result

    def test_multiple_top_level_objects_returns_first(self) -> None:
        first = (
            '{"kind":"tool_call","tool_name":"execute_bash","tool_input":"ls","final_output":null}'
        )
        raw = f'{{"kind":"thought","text":"thinking"}} {first}'
        result = _extract_json(raw)
        assert result == '{"kind":"thought","text":"thinking"}'

    def test_string_containing_braces_not_confused(self) -> None:
        # tool_input contains braces that must not confuse brace counting
        raw = (
            '{"kind":"tool_call","tool_name":"execute_bash"'
            ',"tool_input":"cat {a,b}.py","final_output":null}'
        )
        result = _extract_json(raw)
        assert result == raw

    def test_non_json_braces_in_prose_skipped(self) -> None:
        # Prose containing a non-JSON balanced brace before the real object
        valid = '{"kind":"final","tool_name":null,"tool_input":null,"final_output":"ok"}'
        raw = f"This is a set {{a, b}}. The tool call is {valid}"
        result = _extract_json(raw)
        assert result == valid


class TestBuildAdapterPrompt:
    def test_prompt_includes_transcript_heading(self) -> None:
        messages = [CliRuntimeMessage(role="system", content="Be a coding agent.")]
        prompt = _build_adapter_prompt(messages)
        assert "## Runtime Transcript" in prompt

    def test_prompt_includes_message_content(self) -> None:
        messages = [CliRuntimeMessage(role="system", content="Special instruction here.")]
        prompt = _build_adapter_prompt(messages)
        assert "Special instruction here." in prompt

    def test_prompt_includes_tool_message_heading(self) -> None:
        messages = [
            CliRuntimeMessage(role="system", content="Go."),
            CliRuntimeMessage(role="tool", tool_name="execute_bash", content="Exit code: 0"),
        ]
        prompt = _build_adapter_prompt(messages)
        assert "tool:execute_bash" in prompt

    def test_prompt_instructs_json_only_output(self) -> None:
        messages = [CliRuntimeMessage(role="system", content="Go.")]
        prompt = _build_adapter_prompt(messages)
        assert "No markdown fences" in prompt or "no markdown fences" in prompt.lower()

    def test_prompt_preserves_adapter_rules_with_worker_system_prompt(self) -> None:
        messages = [CliRuntimeMessage(role="system", content="Go.")]
        prompt = _build_adapter_prompt(messages, system_prompt="Reviewer instructions")
        assert "## Worker System Prompt" in prompt
        assert "Reviewer instructions" in prompt
        assert "Choose one of two actions:" in prompt


class TestCoercePositiveInt:
    def test_bool_returns_default(self) -> None:
        assert _coerce_positive_int(True, default=5) == 5  # noqa: FBT003

    def test_positive_int_returned_as_is(self) -> None:
        assert _coerce_positive_int(42, default=5) == 42

    def test_zero_int_returns_default(self) -> None:
        assert _coerce_positive_int(0, default=5) == 5

    def test_negative_int_returns_default(self) -> None:
        assert _coerce_positive_int(-3, default=5) == 5

    def test_positive_float_returns_truncated_int(self) -> None:
        assert _coerce_positive_int(30.9, default=5) == 30

    def test_zero_float_returns_default(self) -> None:
        assert _coerce_positive_int(0.0, default=5) == 5

    def test_string_integer_parsed(self) -> None:
        assert _coerce_positive_int("60", default=5) == 60

    def test_empty_string_returns_default(self) -> None:
        assert _coerce_positive_int("", default=5) == 5

    def test_whitespace_string_returns_default(self) -> None:
        assert _coerce_positive_int("  ", default=5) == 5

    def test_non_numeric_string_returns_default(self) -> None:
        assert _coerce_positive_int("abc", default=5) == 5

    def test_zero_string_returns_default(self) -> None:
        assert _coerce_positive_int("0", default=5) == 5

    def test_none_returns_default(self) -> None:
        assert _coerce_positive_int(None, default=5) == 5

    def test_list_returns_default(self) -> None:
        assert _coerce_positive_int([], default=5) == 5


class TestTruncateDetail:
    def test_empty_string_returns_placeholder(self) -> None:
        assert _truncate_detail("") == "<empty>"

    def test_whitespace_only_returns_placeholder(self) -> None:
        assert _truncate_detail("   ") == "<empty>"

    def test_short_string_returned_as_is(self) -> None:
        assert _truncate_detail("hello") == "hello"

    def test_long_string_is_truncated(self) -> None:
        long_text = "x" * 2000
        result = _truncate_detail(long_text)
        assert result.startswith("[truncated]")


def test_gemini_adapter_extracts_response_field_from_json(monkeypatch) -> None:
    """When the CLI returns JSON, the 'response' field should be used if present."""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 0, stdout='{"response": "Extracted text"}', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = GeminiCliRuntimeAdapter()
    # next_step will then try to parse "Extracted text" as CliRuntimeStep JSON
    # so we expect a fallback to 'final' with "Extracted text"
    step = adapter.next_step([CliRuntimeMessage(role="system", content="Go.")])
    assert step.kind == "final"
    assert step.final_output == "Extracted text"


def test_gemini_adapter_raises_on_empty_response(monkeypatch) -> None:
    """An empty response from the CLI should raise RuntimeError."""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="  ", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = GeminiCliRuntimeAdapter()
    with pytest.raises(RuntimeError, match="empty response body"):
        adapter.next_step([CliRuntimeMessage(role="system", content="Go.")])


def test_gemini_adapter_handles_complex_tool_input_json(monkeypatch) -> None:
    """When tool_input is a JSON object in the response, it should be stringified."""

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout='{"kind":"tool_call","tool_name":"test","tool_input":{"key":"val"}}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = GeminiCliRuntimeAdapter()
    step = adapter.next_step([CliRuntimeMessage(role="system", content="Go.")])
    assert step.kind == "tool_call"
    assert step.tool_input == '{"key": "val"}'
