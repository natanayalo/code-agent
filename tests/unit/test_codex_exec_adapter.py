"""Unit tests for the concrete Codex CLI runtime adapter."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from workers.cli_runtime import CliRuntimeMessage
from workers.codex_exec_adapter import (
    CodexExecCliRuntimeAdapter,
    _build_adapter_prompt,
    _codex_output_schema,
)


def test_codex_exec_adapter_invokes_codex_exec_and_parses_a_tool_call(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """The adapter should shell out once and parse the schema-constrained last message."""
    recorded: dict[str, object] = {}

    def fake_run(
        command: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: int,
        env: dict[str, str] | None,
    ) -> subprocess.CompletedProcess[str]:
        recorded["command"] = list(command)
        recorded["input"] = input
        recorded["timeout"] = timeout
        recorded["env"] = env
        output_path = Path(command[command.index("--output-last-message") + 1])
        schema_path = Path(command[command.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert schema == _codex_output_schema()
        output_path.write_text(
            json.dumps(
                {
                    "kind": "tool_call",
                    "tool_name": "execute_bash",
                    "tool_input": "pytest -q",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command, 0, stdout='{"type":"turn.started"}\n', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = CodexExecCliRuntimeAdapter(
        executable="/opt/codex",
        model="gpt-5.4",
        profile="ci",
        request_timeout_seconds=45,
        working_directory=tmp_path,
    )

    step = adapter.next_step(
        [
            CliRuntimeMessage(role="system", content="You are a coding agent."),
            CliRuntimeMessage(
                role="tool",
                tool_name="execute_bash",
                content=("Tool result: execute_bash\nExit code: 1\nOutput:\n```text\nboom\n```"),
            ),
        ]
    )

    assert step.kind == "tool_call"
    assert step.tool_name == "execute_bash"
    assert step.tool_input == "pytest -q"
    assert recorded["timeout"] == 45
    assert isinstance(recorded["env"], dict)
    command = recorded["command"]
    assert isinstance(command, list)
    assert command[0:8] == [
        "/opt/codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-schema",
    ]
    assert "--output-last-message" in command
    assert "--ephemeral" in command
    assert command[command.index("-C") + 1] == str(tmp_path)
    assert command[-5:] == ["--model", "gpt-5.4", "--profile", "ci", "-"]
    assert "## Runtime Transcript" in str(recorded["input"])
    assert "Tool result: execute_bash" in str(recorded["input"])


def test_codex_exec_adapter_surfaces_cli_failures(monkeypatch, tmp_path: Path) -> None:
    """CLI stderr/stdout should be included when the subprocess fails."""

    def fake_run(
        command,
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: int,
        env,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="json event stream",
            stderr="authentication missing",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    adapter = CodexExecCliRuntimeAdapter(working_directory=tmp_path)

    with pytest.raises(RuntimeError, match="authentication missing"):
        adapter.next_step([CliRuntimeMessage(role="system", content="Return the next step.")])


def test_codex_exec_adapter_from_env_applies_supported_overrides() -> None:
    """Environment variables should map into the concrete adapter settings."""
    adapter = CodexExecCliRuntimeAdapter.from_env(
        {
            "CODE_AGENT_CODEX_CLI_BIN": "/usr/local/bin/codex",
            "CODE_AGENT_CODEX_MODEL": "gpt-5.4-mini",
            "CODE_AGENT_CODEX_PROFILE": "personal",
            "CODE_AGENT_CODEX_TIMEOUT_SECONDS": "33",
            "CODE_AGENT_CODEX_SANDBOX": "read-only",
            "PATH": "/usr/local/bin:/usr/bin",
            "OPENAI_API_KEY": "key-123",
            "UNRELATED_SECRET": "must-not-pass",
        }
    )

    assert adapter.executable == "/usr/local/bin/codex"
    assert adapter.model == "gpt-5.4-mini"
    assert adapter.profile == "personal"
    assert adapter.request_timeout_seconds == 33
    assert adapter.sandbox_mode == "read-only"
    assert adapter.env == {
        "PATH": "/usr/local/bin:/usr/bin",
        "OPENAI_API_KEY": "key-123",
    }


def test_codex_exec_adapter_scopes_constructor_default_env(monkeypatch, tmp_path: Path) -> None:
    """Direct construction should still scope subprocess env vars by default."""
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "key-123")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")

    adapter = CodexExecCliRuntimeAdapter(working_directory=tmp_path)

    assert adapter.env["PATH"] == "/usr/local/bin:/usr/bin"
    assert adapter.env["OPENAI_API_KEY"] == "key-123"
    assert "UNRELATED_SECRET" not in adapter.env


def test_codex_prompt_keeps_adapter_rules_when_worker_system_prompt_is_provided() -> None:
    """Providing a worker system prompt must not remove adapter JSON/tool rules."""
    prompt = _build_adapter_prompt(
        [CliRuntimeMessage(role="system", content="System message.")],
        system_prompt="Reviewer instructions",
    )

    assert "## Worker System Prompt" in prompt
    assert "Reviewer instructions" in prompt
    assert "Choose one of two actions:" in prompt
    assert "Use only tool names listed in the system prompt's Available Tools section." in prompt
