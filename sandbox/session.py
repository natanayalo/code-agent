"""Persistent shell session support for long-lived sandbox containers."""

from __future__ import annotations

import logging
import shlex
import subprocess
import threading
from time import perf_counter
from typing import IO, Protocol
from uuid import uuid4

from pydantic import Field

from sandbox.container import DockerSandboxContainer
from sandbox.streams import MAX_OUTPUT_SIZE_BYTES, decode_bounded, read_stream_bounded
from sandbox.workspace import SandboxModel, _mask_url_credentials

logger = logging.getLogger(__name__)


class ShellProcessFactory(Protocol):
    """Protocol for starting a long-lived shell process."""

    def __call__(self, command: list[str]) -> subprocess.Popen[bytes]: ...


class DockerShellCommandResult(SandboxModel):
    """Captured result for one command executed in a persistent shell session."""

    command: str = Field(min_length=1)
    output: str
    exit_code: int
    duration_seconds: float = Field(ge=0)


class DockerShellSessionError(RuntimeError):
    """Raised when a persistent shell session fails."""


class _ExitMarkerStream:
    """Expose one command's output as a bounded stream terminated by a marker line."""

    def __init__(
        self,
        stream: IO[bytes],
        *,
        tail: bytearray,
        marker_prefix: bytes,
    ) -> None:
        self._stream = stream
        self._tail = tail
        self._marker_prefix = marker_prefix
        self.exit_code: int | None = None
        self.marker_seen = False
        self._exhausted = False

    def _read_from_stream(self) -> bytes:
        """Read the next available chunk without waiting for EOF when possible."""
        read1 = getattr(self._stream, "read1", None)
        if callable(read1):
            return read1(65536)
        return self._stream.read(65536)

    def _try_extract_marker(self) -> tuple[int, int] | None:
        marker_index = self._tail.find(self._marker_prefix)
        if marker_index == -1:
            return None
        newline_index = self._tail.find(b"\n", marker_index + len(self._marker_prefix))
        if newline_index == -1:
            return None

        exit_code_bytes = self._tail[marker_index + len(self._marker_prefix) : newline_index]
        try:
            self.exit_code = int(exit_code_bytes.decode("ascii"))
        except ValueError as exc:
            raise DockerShellSessionError(
                f"Persistent shell session returned an invalid exit code marker: "
                f"{exit_code_bytes!r}"
            ) from exc
        return marker_index, newline_index + 1

    def read(self, n: int = -1) -> bytes:
        if self._exhausted:
            return b""

        while True:
            marker_span = self._try_extract_marker()
            if marker_span is not None:
                marker_index, marker_end = marker_span
                if marker_index > 0:
                    chunk_size = marker_index if n < 0 else min(n, marker_index)
                    chunk = bytes(self._tail[:chunk_size])
                    del self._tail[:chunk_size]
                    return chunk
                del self._tail[:marker_end]
                self.marker_seen = True
                self._exhausted = True
                return b""

            safe_keep = max(len(self._marker_prefix), 1)
            emit_len = len(self._tail) - safe_keep
            if emit_len > 0:
                chunk_size = emit_len if n < 0 else min(n, emit_len)
                chunk = bytes(self._tail[:chunk_size])
                del self._tail[:chunk_size]
                return chunk

            chunk = self._read_from_stream()
            if chunk == b"":
                if self._tail:
                    chunk_size = len(self._tail) if n < 0 else min(n, len(self._tail))
                    buffered = bytes(self._tail[:chunk_size])
                    del self._tail[:chunk_size]
                    if not self._tail:
                        self._exhausted = True
                    return buffered
                self._exhausted = True
                return b""

            self._tail.extend(chunk)


def _build_shell_bootstrap_command(
    container: DockerSandboxContainer,
    *,
    docker_binary: str,
    shell_binary: str,
) -> list[str]:
    """Build the `docker exec` command that starts the persistent shell."""
    quoted_workdir = shlex.quote(container.working_dir)
    quoted_shell = shlex.quote(shell_binary)
    return [
        docker_binary,
        "exec",
        "-i",
        container.container_name,
        "/bin/sh",
        "-lc",
        f"cd {quoted_workdir} && exec {quoted_shell}",
    ]


def _default_shell_process_factory(command: list[str]) -> subprocess.Popen[bytes]:
    """Start the long-lived shell session process."""
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        cmd_str = _mask_url_credentials(shlex.join(command))
        raise DockerShellSessionError(
            f"Failed to start persistent shell session ({cmd_str}): {exc}"
        ) from exc


class DockerShellSession:
    """Maintain a single long-lived shell process inside a sandbox container."""

    def __init__(
        self,
        container: DockerSandboxContainer,
        *,
        docker_binary: str = "docker",
        shell_binary: str = "/bin/sh",
        output_limit_bytes: int = MAX_OUTPUT_SIZE_BYTES,
        close_timeout_seconds: int = 5,
        process_factory: ShellProcessFactory | None = None,
    ) -> None:
        self.container = container
        self.docker_binary = docker_binary
        self.shell_binary = shell_binary
        self.output_limit_bytes = output_limit_bytes
        self.close_timeout_seconds = close_timeout_seconds
        self._lock = threading.Lock()
        self._stdout_tail = bytearray()
        self._closed = False

        bootstrap_command = _build_shell_bootstrap_command(
            container,
            docker_binary=docker_binary,
            shell_binary=shell_binary,
        )
        self._process = (process_factory or _default_shell_process_factory)(bootstrap_command)
        stdin = self._process.stdin
        stdout = self._process.stdout
        if stdin is None or stdout is None:
            for pipe in (stdin, stdout):
                try:
                    if pipe is not None:
                        pipe.close()
                except OSError:
                    pass
            raise DockerShellSessionError("Persistent shell session did not expose stdin/stdout.")
        self._stdin = stdin
        self._stdout = stdout

        logger.info(
            "Started persistent shell session",
            extra={
                "workspace_id": container.workspace.workspace_id,
                "task_id": container.workspace.task_id,
                "container_name": container.container_name,
            },
        )

    def __enter__(self) -> DockerShellSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _close_process_pipes(self) -> None:
        for pipe in (self._stdin, self._stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    def _terminate_process(self) -> None:
        if self._process.poll() is None:
            self._process.kill()
            try:
                self._process.wait(timeout=self.close_timeout_seconds)
            except subprocess.TimeoutExpired:
                pass
        self._close_process_pipes()
        self._closed = True

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        """Execute one shell command while preserving session state for later commands."""
        if not command.strip():
            raise DockerShellSessionError("Persistent shell session requires a non-empty command.")

        with self._lock:
            if self._closed or self._process.poll() is not None:
                raise DockerShellSessionError("Persistent shell session is closed.")

            marker = f"__CODE_AGENT_EXIT_{uuid4().hex}__"
            marker_prefix = ("\n" + marker + " ").encode("ascii")
            stream = _ExitMarkerStream(
                self._stdout,
                tail=self._stdout_tail,
                marker_prefix=marker_prefix,
            )

            wrapped_command = (
                command.rstrip("\n")
                + "\n"
                + "__code_agent_status=$?\n"
                + f"printf '\\n{marker} %s\\n' \"$__code_agent_status\"\n"
            ).encode("utf-8")

            started_at = perf_counter()
            try:
                self._stdin.write(wrapped_command)
                self._stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self._terminate_process()
                raise DockerShellSessionError(
                    f"Persistent shell session failed while sending command: {exc}"
                ) from exc

            output_buf = bytearray()
            limit_exceeded = threading.Event()

            def kill_on_limit() -> None:
                limit_exceeded.set()
                self._terminate_process()

            error_holder: list[BaseException] = []

            def read_output() -> None:
                try:
                    output_buf.extend(
                        read_stream_bounded(
                            stream,
                            self.output_limit_bytes,
                            on_limit=kill_on_limit,
                        )
                    )
                except BaseException as exc:  # pragma: no cover - defensive thread handoff
                    error_holder.append(exc)

            reader_thread = threading.Thread(target=read_output, daemon=True)
            reader_thread.start()
            reader_thread.join(timeout_seconds)

            if reader_thread.is_alive():
                self._terminate_process()
                reader_thread.join()
                output = decode_bounded(output_buf, self.output_limit_bytes).strip()
                detail = f" Partial output:\n{output}" if output else ""
                raise DockerShellSessionError(
                    f"Persistent shell session command timed out after {timeout_seconds}s: "
                    f"{command}{detail}"
                )

            if error_holder:
                first_error = error_holder[0]
                if isinstance(first_error, DockerShellSessionError):
                    raise first_error
                raise DockerShellSessionError(
                    f"Persistent shell session failed while reading command output: "
                    f"{first_error}"
                ) from first_error

            output = decode_bounded(output_buf, self.output_limit_bytes)
            if limit_exceeded.is_set():
                raise DockerShellSessionError(
                    f"Persistent shell session output limit exceeded "
                    f"({self.output_limit_bytes} bytes) for command: {command}"
                )
            if not stream.marker_seen or stream.exit_code is None:
                self._terminate_process()
                raise DockerShellSessionError(
                    f"Persistent shell session terminated before returning an exit code for "
                    f"command: {command}\nPartial output:\n{output}"
                )

            return DockerShellCommandResult(
                command=command,
                output=output,
                exit_code=stream.exit_code,
                duration_seconds=perf_counter() - started_at,
            )

    def close(self) -> None:
        """Gracefully end the persistent shell session, killing it if needed."""
        with self._lock:
            if self._closed:
                return

            self._closed = True
            if self._process.poll() is not None:
                self._close_process_pipes()
                return

            try:
                self._stdin.write(b"exit\n")
                self._stdin.flush()
            except (BrokenPipeError, OSError):
                pass

            try:
                self._process.wait(timeout=self.close_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._process.kill()
                try:
                    self._process.wait(timeout=self.close_timeout_seconds)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                self._close_process_pipes()
