"""Unit tests to increase coverage of native_agent_runner."""

import subprocess
from pathlib import Path

from sandbox.native_agent_executor import (
    NativeAgentExecution,
    NativeAgentExecutorError,
)
from workers.native_agent_runner import (
    NativeAgentRunRequest,
    _determine_exit_status,
    _handle_native_agent_timeout,
    run_native_agent,
)


def test_extract_status_and_summary_econnreset():
    status, summary, reports = _determine_exit_status(
        completed_returncode=1,
        stdout_text="some stdout",
        stderr_text="ECONNRESET happened",
        final_message="",
    )
    assert status == "error"
    assert "network retry exhaustion" in summary


def test_handle_native_agent_timeout_network_error():
    request = NativeAgentRunRequest(
        command=["echo"],
        prompt="echo",
        repo_path=Path("/tmp/repo"),
        workspace_path=Path("/tmp/workspace"),
        timeout_seconds=5,
    )

    # Exceed max retries but with a network error output
    exc = subprocess.TimeoutExpired(["echo"], 5)
    exc.stdout = b"ECONNRESET"
    exc.stderr = b""
    should_retry, result = _handle_native_agent_timeout(
        request=request,
        exc=exc,
        retry_count=1,
        max_retries=3,
        command_text="echo",
        started_at=0.0,
        artifact_root=Path("/tmp/artifacts"),
        events_path=Path("/tmp/events.json"),
        provider_log_path=None,
    )
    assert should_retry is True
    assert result is None

    # Exceed max retries
    exc = subprocess.TimeoutExpired(["echo"], 5)
    exc.stdout = b"ECONNRESET"
    exc.stderr = b""
    should_retry, result = _handle_native_agent_timeout(
        request=request,
        exc=exc,
        retry_count=3,
        max_retries=3,
        command_text="echo",
        started_at=0.0,
        artifact_root=Path("/tmp/artifacts"),
        events_path=Path("/tmp/events.json"),
        provider_log_path=None,
    )
    assert should_retry is False
    assert result is not None
    assert result.status == "error"


class FakeRunnerTimeout:
    def run(self, *args, **kwargs):
        class Completed:
            stdout = ""
            stderr = ""
            returncode = 1

        return NativeAgentExecution(
            completed=Completed(), termination_reason="timeout", manifest_path=Path("foo")
        )


def test_run_native_agent_executor_timeout_output(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    request = NativeAgentRunRequest(
        command=["echo"],
        prompt="echo",
        repo_path=repo,
        workspace_path=ws,
        timeout_seconds=5,
        process_runner=FakeRunnerTimeout(),
    )

    result = run_native_agent(request)
    assert result.timed_out is True
    assert result.status == "error"


class FakeRunnerError:
    def run(self, *args, **kwargs):
        raise NativeAgentExecutorError("executor failed")


def test_run_native_agent_executor_error_handling(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    request = NativeAgentRunRequest(
        command=["echo"],
        prompt="echo",
        repo_path=repo,
        workspace_path=ws,
        timeout_seconds=5,
        process_runner=FakeRunnerError(),
    )

    result = run_native_agent(request)
    assert result.status == "error"
    assert "executor failed" in result.summary
