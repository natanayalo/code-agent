"""Unit tests for native-agent tracing guardrails."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sandbox.redact import SecretRedactor
from workers.native_agent_runner import NativeAgentRunRequest, run_native_agent


@pytest.fixture
def mock_tracing():
    with (
        patch("workers.native_agent_runner.start_optional_span") as mock_start,
        patch("workers.native_agent_runner.set_current_span_attribute") as mock_set_attr,
        patch("workers.native_agent_runner.set_span_input_output") as mock_set_io,
    ):
        span = MagicMock()
        mock_start.return_value.__enter__.return_value = span

        yield {"span": span, "set_attr": mock_set_attr, "set_io": mock_set_io}


def test_run_native_agent_emits_redacted_span(tmp_path: Path, mock_tracing) -> None:
    """Run native agent should emit a span with redacted and truncated attributes."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    # Mock subprocess.run to return a simple success
    def side_effect(*args, **kwargs):
        completed = MagicMock()
        completed.returncode = 0
        if kwargs.get("text"):
            completed.stdout = "hello world"
            completed.stderr = ""
        else:
            completed.stdout = b"hello world"
            completed.stderr = b""
        return completed

    redactor = SecretRedactor(secrets=["SENSITIVE_TOKEN"])

    with patch("subprocess.run", side_effect=side_effect):
        run_native_agent(
            NativeAgentRunRequest(
                command=["echo", "SENSITIVE_TOKEN"],
                prompt="Use SENSITIVE_TOKEN to do stuff",
                repo_path=repo_path,
                workspace_path=tmp_path,
                timeout_seconds=10,
                redactor=redactor,
            )
        )

    # Check set_io calls
    # set_span_input_output signature is (input_data=None, output_data=None)

    inputs = []
    outputs = []
    for call in mock_tracing["set_io"].call_args_list:
        if "input_data" in call.kwargs:
            inputs.append(call.kwargs["input_data"])
        elif len(call.args) > 0:
            inputs.append(call.args[0])

        if "output_data" in call.kwargs:
            outputs.append(call.kwargs["output_data"])
        elif len(call.args) > 1:
            outputs.append(call.args[1])

    assert any("Use [REDACTED] to do stuff" in (s or "") for s in inputs)

    # Check set_attr calls
    attr_calls = {call.args[0]: call.args[1] for call in mock_tracing["set_attr"].call_args_list}
    assert attr_calls["code_agent.native_agent.command"] == "echo [REDACTED]"


def test_run_native_agent_truncates_noisy_streams(tmp_path: Path, mock_tracing) -> None:
    """Large stdout/stderr should be truncated in span attributes."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    long_output = "noisy log line\n" * 500  # Way over 2000 chars

    def side_effect(*args, **kwargs):
        completed = MagicMock()
        completed.returncode = 0
        if kwargs.get("text"):
            completed.stdout = long_output
            completed.stderr = "short error"
        else:
            completed.stdout = b""
            completed.stderr = b""
        return completed

    with patch("subprocess.run", side_effect=side_effect):
        run_native_agent(
            NativeAgentRunRequest(
                command=["noisy_cmd"],
                prompt="task",
                repo_path=repo_path,
                workspace_path=tmp_path,
                timeout_seconds=10,
            )
        )

    attr_calls = {call.args[0]: call.args[1] for call in mock_tracing["set_attr"].call_args_list}

    stdout_attr = attr_calls["code_agent.native_agent.stdout"]
    assert len(stdout_attr) <= 2500  # Roughly 2000 + truncation marker
    assert "[TRUNCATED: Output exceeded 2000 characters]" in stdout_attr


def test_run_native_agent_sets_error_status_on_timeout(tmp_path: Path, mock_tracing) -> None:
    """Timeouts should result in terminal error status on the span."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["sleep"], timeout=1)):
        with patch("workers.native_agent_runner.set_span_status_from_outcome") as mock_set_status:
            run_native_agent(
                NativeAgentRunRequest(
                    command=["sleep", "10"],
                    prompt="task",
                    repo_path=repo_path,
                    workspace_path=tmp_path,
                    timeout_seconds=1,
                )
            )

    attr_calls = {call.args[0]: call.args[1] for call in mock_tracing["set_attr"].call_args_list}
    assert attr_calls["code_agent.native_agent.timed_out"] is True
    assert "code_agent.native_agent.stdout" in attr_calls
    assert "code_agent.native_agent.stderr" in attr_calls

    # Check set_io for output_data
    outputs = []
    for call in mock_tracing["set_io"].call_args_list:
        if "output_data" in call.kwargs:
            outputs.append(call.kwargs["output_data"])
        elif len(call.args) > 1:
            outputs.append(call.args[1])

    assert any("Native agent command timed out" in (s or "") for s in outputs)

    # Status should be set
    mock_set_status.assert_called_once()
    assert mock_set_status.call_args[0][0] == "error"
