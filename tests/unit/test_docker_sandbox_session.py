"""Unit tests for persistent sandbox shell sessions."""

from __future__ import annotations

import io
import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sandbox.container import DockerSandboxContainer
from sandbox.session import (
    DockerShellSession,
    DockerShellSessionError,
    _default_shell_process_factory,
    _ExitMarkerStream,
)
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle


def _workspace_handle(tmp_path: Path) -> WorkspaceHandle:
    workspace_path = tmp_path / "workspace-task-45"
    repo_path = workspace_path / "repo"
    repo_path.mkdir(parents=True)
    return WorkspaceHandle(
        workspace_id="workspace-task-45",
        task_id="task-45",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


def _container_handle(tmp_path: Path) -> DockerSandboxContainer:
    return DockerSandboxContainer(
        workspace=_workspace_handle(tmp_path),
        container_name="sandbox-workspace-task-45",
        image="python:3.12-slim",
    )


def _local_shell_process_factory(command: list[str]) -> subprocess.Popen[bytes]:
    del command
    return subprocess.Popen(
        ["/bin/sh"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


class _ChunkStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, size: int = -1) -> bytes:
        del size
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeInputPipe:
    def __init__(
        self,
        *,
        write_error: BaseException | None = None,
        flush_error: BaseException | None = None,
        close_error: OSError | None = None,
    ) -> None:
        self.write_error = write_error
        self.flush_error = flush_error
        self.close_error = close_error
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        return len(data)

    def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _FakeProcess:
    def __init__(
        self,
        *,
        stdin: object,
        stdout: object,
        poll_result: int | None = None,
        wait_results: list[object] | None = None,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self._poll_result = poll_result
        self._wait_results = list(wait_results or [0])
        self.killed = False

    def poll(self) -> int | None:
        return self._poll_result

    def wait(self, timeout: int | None = None) -> int:
        del timeout
        result = self._wait_results.pop(0) if self._wait_results else 0
        if isinstance(result, BaseException):
            raise result
        return int(result)

    def kill(self) -> None:
        self.killed = True
        self._poll_result = 1


def _session_with_fake_process(process: _FakeProcess, tmp_path: Path) -> DockerShellSession:
    container = _container_handle(tmp_path)
    return DockerShellSession(container, process_factory=lambda command: process)


def test_exit_marker_stream_reads_without_read1_and_flushes_tail() -> None:
    """The marker stream should fall back to read() and eventually flush leftover bytes."""
    marker_prefix = b"\nMARKER "
    stream = _ExitMarkerStream(
        _ChunkStream([b"hello world", b""]),
        tail=bytearray(),
        marker_prefix=marker_prefix,
    )

    chunks: list[bytes] = []
    while True:
        chunk = stream.read(3)
        if not chunk:
            break
        chunks.append(chunk)

    assert b"".join(chunks) == b"hello world"


def test_exit_marker_stream_returns_empty_after_marker_consumed() -> None:
    """Once a marker is consumed, future reads should return EOF immediately."""
    stream = _ExitMarkerStream(
        io.BytesIO(b""),
        tail=bytearray(b"\nMARKER 0\n"),
        marker_prefix=b"\nMARKER ",
    )

    assert stream.read() == b""
    assert stream.read() == b""
    assert stream.marker_seen is True
    assert stream.exit_code == 0


def test_exit_marker_stream_returns_none_when_marker_newline_is_incomplete() -> None:
    """An unterminated marker should not be parsed until the trailing newline arrives."""
    stream = _ExitMarkerStream(
        io.BytesIO(b""),
        tail=bytearray(b"\nMARKER 0"),
        marker_prefix=b"\nMARKER ",
    )

    assert stream._try_extract_marker() is None


def test_exit_marker_stream_raises_on_invalid_exit_code_marker() -> None:
    """Non-integer exit markers should raise a session error."""
    stream = _ExitMarkerStream(
        io.BytesIO(b""),
        tail=bytearray(b"\nMARKER nope\n"),
        marker_prefix=b"\nMARKER ",
    )

    with pytest.raises(DockerShellSessionError, match="invalid exit code marker"):
        stream._try_extract_marker()


def test_default_shell_process_factory_raises_on_os_error() -> None:
    """OS failures while starting the shell should surface as session errors."""
    with patch("subprocess.Popen", side_effect=OSError("shell missing")):
        with pytest.raises(
            DockerShellSessionError,
            match=r"Failed to start persistent shell session .* shell missing",
        ):
            _default_shell_process_factory(["docker", "exec", "-i", "sandbox"])


def test_shell_session_rejects_missing_pipes(tmp_path: Path) -> None:
    """Session construction should fail if the shell process does not expose pipes."""
    process = _FakeProcess(stdin=None, stdout=io.BytesIO())

    with pytest.raises(DockerShellSessionError, match="did not expose stdin/stdout"):
        _session_with_fake_process(process, tmp_path)


def test_shell_session_rejects_blank_command(tmp_path: Path) -> None:
    """Blank shell commands should fail before any session I/O occurs."""
    session = DockerShellSession(
        _container_handle(tmp_path), process_factory=_local_shell_process_factory
    )

    try:
        with pytest.raises(DockerShellSessionError, match="non-empty command"):
            session.execute("   ")
    finally:
        session.close()


def test_shell_session_preserves_environment_and_working_directory(tmp_path: Path) -> None:
    """Environment exports and directory changes should persist across commands."""
    container = _container_handle(tmp_path)

    with DockerShellSession(container, process_factory=_local_shell_process_factory) as session:
        first = session.execute("export GREETING=hello")
        second = session.execute('printf "%s\\n" "$GREETING"')
        session.execute(f"cd {shlex.quote(str(tmp_path))}")
        third = session.execute("pwd")
        fourth = session.execute("printf 'done\\n'")

    assert first.exit_code == 0
    assert first.output == ""
    assert second.output == "hello\n"
    assert third.output.strip() == str(tmp_path)
    assert fourth.output == "done\n"


def test_shell_session_handles_command_ending_with_trailing_backslash(tmp_path: Path) -> None:
    """Wrapper bookkeeping should not be folded into a trailing line continuation."""
    container = _container_handle(tmp_path)

    with DockerShellSession(container, process_factory=_local_shell_process_factory) as session:
        result = session.execute("printf 'value' \\")
        follow_up = session.execute("printf 'done\\n'")

    assert result.exit_code == 0
    assert result.output == "value"
    assert follow_up.output == "done\n"


def test_shell_session_times_out_and_closes(tmp_path: Path) -> None:
    """A timed-out command should kill the session instead of hanging indefinitely."""
    container = _container_handle(tmp_path)

    session = DockerShellSession(container, process_factory=_local_shell_process_factory)
    with pytest.raises(DockerShellSessionError, match="timed out after 1s"):
        session.execute("sleep 2", timeout_seconds=1)

    with pytest.raises(DockerShellSessionError, match="closed"):
        session.execute("printf 'still here\\n'")


def test_shell_session_enforces_output_limit(tmp_path: Path) -> None:
    """Command output should be bounded to avoid runaway shell sessions."""
    container = _container_handle(tmp_path)

    session = DockerShellSession(
        container,
        process_factory=_local_shell_process_factory,
        output_limit_bytes=32,
    )
    with pytest.raises(DockerShellSessionError, match="output limit exceeded"):
        session.execute("printf 'abcdefghijklmnopqrstuvwxyz0123456789'")
    session.close()


def test_shell_session_raises_when_sending_command_fails(tmp_path: Path) -> None:
    """Broken shell stdin should surface as a send failure and terminate the process."""
    process = _FakeProcess(
        stdin=_FakeInputPipe(write_error=BrokenPipeError("boom")),
        stdout=_FakeInputPipe(close_error=OSError("close failed")),
        wait_results=[subprocess.TimeoutExpired(cmd="shell", timeout=5)],
    )
    session = _session_with_fake_process(process, tmp_path)

    with pytest.raises(DockerShellSessionError, match="failed while sending command"):
        session.execute("printf 'hi\\n'")

    assert process.killed is True


def test_shell_session_raises_when_reader_thread_errors(tmp_path: Path) -> None:
    """Unexpected read failures should be surfaced to the caller."""

    class _ExplodingStream:
        def read(self, size: int = -1) -> bytes:
            del size
            raise RuntimeError("read exploded")

        def close(self) -> None:
            return None

    process = _FakeProcess(
        stdin=_FakeInputPipe(),
        stdout=_ExplodingStream(),
    )
    session = _session_with_fake_process(process, tmp_path)

    with pytest.raises(DockerShellSessionError, match="failed while reading command output"):
        session.execute("printf 'hi\\n'")


def test_shell_session_raises_when_command_finishes_without_exit_marker(tmp_path: Path) -> None:
    """Missing exit markers should be treated as a broken session response."""
    process = _FakeProcess(
        stdin=_FakeInputPipe(),
        stdout=io.BytesIO(b"partial output\n"),
    )
    session = _session_with_fake_process(process, tmp_path)

    with pytest.raises(DockerShellSessionError, match="terminated before returning an exit code"):
        session.execute("printf 'hi\\n'")

    assert process.killed is True


def test_shell_session_close_is_idempotent_for_already_exited_process(tmp_path: Path) -> None:
    """Closing an already-exited process should just close pipes and return."""
    stdin = _FakeInputPipe()
    stdout = io.BytesIO()
    process = _FakeProcess(stdin=stdin, stdout=stdout, poll_result=0)
    session = _session_with_fake_process(process, tmp_path)

    session.close()
    session.close()

    assert stdin.closed is True


def test_shell_session_close_handles_broken_pipe_and_wait_timeouts(tmp_path: Path) -> None:
    """Close should tolerate exit write failures and repeated wait timeouts."""
    process = _FakeProcess(
        stdin=_FakeInputPipe(write_error=BrokenPipeError("closed")),
        stdout=io.BytesIO(),
        wait_results=[
            subprocess.TimeoutExpired(cmd="shell", timeout=5),
            subprocess.TimeoutExpired(cmd="shell", timeout=5),
        ],
    )
    session = _session_with_fake_process(process, tmp_path)

    session.close()

    assert process.killed is True
