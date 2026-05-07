import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from workers.native_agent_runner import (
    NativeAgentRunRequest,
    _extract_final_message,
    _read_final_message,
    _stdout_fallback_final_message,
    run_native_agent,
)


def test_extract_final_message_raw_string():
    assert _extract_final_message("Hello world") == "Hello world"
    assert _extract_final_message("  Hello world  ") == "Hello world"


def test_extract_final_message_json_string():
    assert _extract_final_message(json.dumps("Hello JSON")) == "Hello JSON"


def test_extract_final_message_json_dict_response():
    payload = {"response": "This is a response"}
    assert _extract_final_message(json.dumps(payload)) == "This is a response"


def test_extract_final_message_json_dict_summary():
    payload = {"summary": "This is a summary"}
    assert _extract_final_message(json.dumps(payload)) == "This is a summary"


def test_extract_final_message_json_dict_message():
    payload = {"message": "This is a message"}
    assert _extract_final_message(json.dumps(payload)) == "This is a message"


def test_extract_final_message_json_dict_final_output():
    payload = {"final_output": "This is final output"}
    assert _extract_final_message(json.dumps(payload)) == "This is final output"


def test_extract_final_message_json_dict_error():
    payload = {"error": "Simple error message"}
    assert _extract_final_message(json.dumps(payload)) == "Simple error message"


def test_extract_final_message_json_dict_structured_error():
    payload = {
        "error": {
            "type": "rate_limit_exceeded",
            "message": "Too many requests",
        }
    }
    assert _extract_final_message(json.dumps(payload)) == "rate_limit_exceeded: Too many requests"


def test_extract_final_message_json_dict_unknown_fallback():
    payload = {"unknown": "field"}
    raw = json.dumps(payload)
    assert _extract_final_message(raw) == raw


def test_stdout_fallback_final_message_json():
    stdout = json.dumps({"response": "Extracted from stdout"})
    assert _stdout_fallback_final_message(stdout) == "Extracted from stdout"


def test_stdout_fallback_final_message_json_in_tail():
    stdout = "Some logs here\n" + json.dumps({"response": "Extracted from tail"})
    assert _stdout_fallback_final_message(stdout) == "Extracted from tail"


def test_stdout_fallback_final_message_plain():
    stdout = "Plain text output"
    assert _stdout_fallback_final_message(stdout) == "Plain text output"


def test_stdout_fallback_final_message_truncation():
    long_stdout = "x" * 2000
    result = _stdout_fallback_final_message(long_stdout)
    assert "[stdout truncated for summary]" in result
    assert len(result) < 1100


def test_read_final_message_from_file(tmp_path):
    msg_file = tmp_path / "final.txt"
    payload = {"response": "File response"}
    msg_file.write_text(json.dumps(payload))
    assert _read_final_message(msg_file) == "File response"


def test_read_final_message_missing_file():
    assert _read_final_message(Path("/non/existent/path")) is None


@patch("subprocess.run")
def test_run_native_agent_success_with_stdout_json(mock_run, tmp_path):
    # First call: main command (text=True)
    # Second call: git status (text=False)
    # Third call: git diff (text=True)
    def side_effect(command, **kwargs):
        if "status" in command:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if "diff" in command:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(
            returncode=0,
            stdout=json.dumps({"response": "Success response"}),
            stderr="",
        )

    mock_run.side_effect = side_effect

    request = NativeAgentRunRequest(
        command=["echo", "test"],
        prompt="test prompt",
        repo_path=tmp_path,
        workspace_path=tmp_path,
    )
    result = run_native_agent(request)
    assert result.status == "success"
    assert result.final_message == "Success response"
    assert result.summary == "Success response"


@patch("subprocess.run")
def test_run_native_agent_failure_with_stdout_json(mock_run, tmp_path):
    def side_effect(command, **kwargs):
        if "status" in command:
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if "diff" in command:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(
            returncode=1,
            stdout=json.dumps({"response": "Failure response"}),
            stderr="Some error",
        )

    mock_run.side_effect = side_effect

    request = NativeAgentRunRequest(
        command=["echo", "test"],
        prompt="test prompt",
        repo_path=tmp_path,
        workspace_path=tmp_path,
    )
    result = run_native_agent(request)
    assert result.status == "failure"
    assert result.final_message == "Failure response"
    assert result.summary == "Native agent command exited with code 1."
