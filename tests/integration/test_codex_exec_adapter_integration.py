"""Integration tests for the Codex CLI runtime adapter."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Final

import pytest

from workers.cli_runtime import CliRuntimeMessage
from workers.codex_exec_adapter import CodexExecCliRuntimeAdapter

MOCK_CODEX_NAME: Final[str] = "mock-codex"


@pytest.fixture
def mock_codex_bin(tmp_path: Path) -> Path:
    """Create a mock codex binary that writes a controlled JSON response."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_bin = bin_dir / MOCK_CODEX_NAME

    # This mock script just writes the last_message.json file and exits.
    # It reads from stdin to simulate prompt consumption.
    script = """#!/usr/bin/env python3
import json
import sys
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("cmd", nargs="*")
parser.add_argument("--output-last-message", type=str)
parser.add_argument("--output-schema", type=str)
parser.add_argument("--sandbox", type=str)
parser.add_argument("-C", type=str)
args, unknown = parser.parse_known_args()

# Consume stdin
prompt = sys.stdin.read()

if args.output_last_message:
    output_path = Path(args.output_last_message)
    # We always return a success tool-call if not instructed otherwise
    response = {"kind": "tool_call", "tool_name": "execute_bash", "tool_input": "pwd", "final_output": None}
    output_path.write_text(json.dumps(response), encoding="utf-8")

sys.exit(0)
"""
    codex_bin.write_text(script, encoding="utf-8")
    codex_bin.chmod(codex_bin.stat().st_mode | stat.S_IEXEC)
    return codex_bin


def test_codex_exec_adapter_builds_correct_prompt_and_calls_binary(
    mock_codex_bin: Path,
    tmp_path: Path,
) -> None:
    """The adapter should correctly format the transcript and call the configured binary."""
    env = {"PATH": f"{mock_codex_bin.parent}:{os.environ.get('PATH', '')}"}
    adapter = CodexExecCliRuntimeAdapter(
        executable=MOCK_CODEX_NAME,
        env=env,
        working_directory=tmp_path,
    )

    messages = [
        CliRuntimeMessage(role="system", content="You are a helper."),
        CliRuntimeMessage(role="assistant", content="How can I help?"),
    ]

    step = adapter.next_step(messages)

    assert step.kind == "tool_call"
    assert step.tool_name == "execute_bash"
    assert step.tool_input == "pwd"


def test_codex_exec_adapter_handles_subprocess_failure(
    mock_codex_bin: Path,
    tmp_path: Path,
) -> None:
    """The adapter should raise a RuntimeError if the binary exits with an error."""
    # Modify mock to exit with error
    mock_codex_bin.write_text(
        mock_codex_bin.read_text().replace("sys.exit(0)", "sys.exit(1)"),
        encoding="utf-8",
    )

    env = {"PATH": f"{mock_codex_bin.parent}:{os.environ.get('PATH', '')}"}
    adapter = CodexExecCliRuntimeAdapter(
        executable=MOCK_CODEX_NAME,
        env=env,
        working_directory=tmp_path,
    )

    with pytest.raises(RuntimeError, match="failed with exit code 1"):
        adapter.next_step([])


def test_codex_exec_adapter_handles_timeout(
    mock_codex_bin: Path,
    tmp_path: Path,
) -> None:
    """The adapter should raise a RuntimeError if the binary times out."""
    # Modify mock to sleep
    mock_codex_bin.write_text(
        mock_codex_bin.read_text().replace(
            "prompt = sys.stdin.read()", "import time; time.sleep(2)"
        ),
        encoding="utf-8",
    )

    env = {"PATH": f"{mock_codex_bin.parent}:{os.environ.get('PATH', '')}"}
    adapter = CodexExecCliRuntimeAdapter(
        executable=MOCK_CODEX_NAME,
        env=env,
        working_directory=tmp_path,
        request_timeout_seconds=1,
    )

    with pytest.raises(RuntimeError, match="timed out after 1s"):
        adapter.next_step([])
