"""Unit tests for the concrete Codex CLI runtime adapter."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from workers.cli_runtime import CliRuntimeMessage
from workers.codex_exec_adapter import CodexExecCliRuntimeAdapter, _codex_output_schema


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
    assert recorded["env"] is None
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
        }
    )

    assert adapter.executable == "/usr/local/bin/codex"
    assert adapter.model == "gpt-5.4-mini"
    assert adapter.profile == "personal"
    assert adapter.request_timeout_seconds == 33
    assert adapter.sandbox_mode == "read-only"
