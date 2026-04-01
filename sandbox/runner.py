"""Docker-based command runner for sandbox workspaces."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import typing
from time import perf_counter
from typing import Protocol

from pydantic import Field

from sandbox.workspace import SandboxModel, WorkspaceHandle, _mask_url_credentials

logger = logging.getLogger(__name__)

# Maximum bytes captured from stdout/stderr. Output beyond this is discarded
# to prevent disk or memory exhaustion from runaway sandbox processes.
MAX_OUTPUT_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB


class DockerCommandRunner(Protocol):
    """Protocol for running docker commands on the host."""

    def __call__(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]: ...


class DockerSandboxCommand(SandboxModel):
    """A request to run a command inside a Docker sandbox."""

    workspace: WorkspaceHandle
    command: list[str] = Field(min_length=1)
    image: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    working_dir: str = "/workspace/repo"
    timeout_seconds: int = Field(default=300, ge=1)
    network_enabled: bool = False
    memory_limit: str | None = "1g"
    cpu_limit: float | None = 1.0


class DockerSandboxResult(SandboxModel):
    """Captured result of a Docker sandbox command."""

    image: str
    command: list[str]
    docker_command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)


class DockerSandboxRunnerError(RuntimeError):
    """Raised when the Docker sandbox cannot start or complete."""


class DockerSandboxOutputLimitError(DockerSandboxRunnerError):
    """Raised when the sandbox exceeds capture limits."""


def _build_container_name(workspace: WorkspaceHandle) -> str:
    """Build a deterministic docker container name for a workspace run."""
    return f"sandbox-{workspace.workspace_id}"


def _extract_container_name(command: list[str]) -> str | None:
    """Return the docker container name embedded in a run command, if present."""
    for index, token in enumerate(command[:-1]):
        if token == "--name":
            return command[index + 1]
    return None


def _kill_docker_container(command: list[str]) -> None:
    """Best-effort docker kill for the named container in *command*."""
    container_name = _extract_container_name(command)
    if container_name is None:
        return

    try:
        subprocess.run(
            [command[0], "kill", container_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning(
            "Failed to stop docker container after sandbox error",
            extra={"container_name": container_name},
        )


def _read_stream_bounded(
    stream: typing.IO[bytes],
    limit: int,
    on_limit: typing.Callable[[], None] | None = None,
) -> bytearray:
    """Read from *stream* into a bytearray, discarding bytes beyond *limit*.

    If *on_limit* is provided, it is invoked if the captured data exceeds *limit*,
    and the stream reading terminates early.

    The stream is drained to its end unless *on_limit* is called, so the subprocess
    pipe never blocks regardless of how much data the container produces.

    Partial data already read is preserved if the stream is closed or an I/O
    error occurs mid-read (e.g. during cleanup to unblock a hanging reader).
    """
    buf = bytearray()
    try:
        for chunk in iter(lambda: stream.read(65536), b""):
            remaining = (limit + 1) - len(buf)
            if remaining > 0:
                buf.extend(chunk[:remaining])
            if len(buf) > limit:
                if on_limit:
                    on_limit()
                    # Stop reading early if the limit was reached—usually because
                    # the process is being killed to prevent further exhaustion.
                    return buf
                # Continue draining the stream to prevent the subprocess from
                # blocking on a full pipe, but discard additional bytes.
    except (OSError, ValueError):
        # Stream closed or became unavailable (e.g. pipe forcibly closed during
        # cleanup).  Return whatever was captured so far.
        pass
    return buf


def _decode_bounded(buf: bytearray, limit: int) -> str:
    """Decode *buf* to a string, appending a truncation marker when needed.

    Decoding the whole buffer (at most limit+1 bytes) before truncating avoids
    splitting a multi-byte UTF-8 sequence at the boundary.
    """
    text = buf.decode("utf-8", errors="replace")
    if len(buf) > limit:
        text = text[:limit] + "\n... (truncated)"
    return text


def _build_docker_run_command(
    request: DockerSandboxCommand,
    *,
    image: str,
    docker_binary: str = "docker",
) -> list[str]:
    """Build a deterministic docker run command for the sandbox request."""
    container_name = _build_container_name(request.workspace)
    command = [
        docker_binary,
        "run",
        "--rm",
        "--name",
        container_name,
    ]
    if request.memory_limit:
        command.extend(["--memory", request.memory_limit])
    if request.cpu_limit:
        command.extend(["--cpus", str(request.cpu_limit)])
    workspace_path = request.workspace.workspace_path.resolve()
    if "," in str(workspace_path):
        raise DockerSandboxRunnerError(
            f"Workspace path contains a comma which is incompatible with "
            f"the --mount syntax: {workspace_path}"
        )
    command.extend(
        [
            "--workdir",
            request.working_dir,
            "--mount",
            f"type=bind,source={workspace_path},target=/workspace",
        ]
    )

    try:
        uid = os.getuid()
        gid = os.getgid()
        command.extend(["--user", f"{uid}:{gid}"])
    except AttributeError:
        pass  # Windows or environment without getuid

    if not request.network_enabled:
        command.extend(["--network", "none"])
    for key, value in sorted(request.environment.items()):
        command.extend(["--env", f"{key}={value}"])
    command.append(image)
    command.extend(request.command)
    return command


# Seconds to wait for reader threads to finish after the process exits.
# If a child process inside the container keeps pipes open, we close them
# explicitly after this window to unblock the threads.
_THREAD_JOIN_TIMEOUT: float = 5.0


def _run_docker_command(
    command: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run docker and safely capture stdout/stderr up to MAX_OUTPUT_SIZE_BYTES.

    Uses Popen with background reader threads so that output is bounded *during*
    execution—preventing both memory exhaustion (RAM) and disk exhaustion that
    would occur if we redirected pipes directly to temporary files on disk.
    """
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        cmd_str = _mask_url_credentials(shlex.join(command))
        raise DockerSandboxRunnerError(
            f"Failed to start Docker sandbox command ({cmd_str}): {exc}"
        ) from exc

    stdout_buf: bytearray = bytearray()
    stderr_buf: bytearray = bytearray()

    assert proc.stdout is not None  # noqa: S101 – guaranteed by PIPE
    assert proc.stderr is not None  # noqa: S101 – guaranteed by PIPE

    limit_exceeded = threading.Event()

    def kill_on_limit() -> None:
        limit_exceeded.set()
        proc.kill()

    stdout_thread = threading.Thread(
        target=lambda: stdout_buf.extend(
            _read_stream_bounded(proc.stdout, MAX_OUTPUT_SIZE_BYTES, on_limit=kill_on_limit)  # type: ignore[arg-type]
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=lambda: stderr_buf.extend(
            _read_stream_bounded(proc.stderr, MAX_OUTPUT_SIZE_BYTES, on_limit=kill_on_limit)  # type: ignore[arg-type]
        ),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    def _join_threads() -> None:
        """Join reader threads, closing pipes if they hang past the timeout."""
        for thread, pipe in [
            (stdout_thread, proc.stdout),
            (stderr_thread, proc.stderr),
        ]:
            thread.join(timeout=_THREAD_JOIN_TIMEOUT)
            if thread.is_alive():
                # Force-close the pipe so the blocked read() unblocks.
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
                thread.join()  # Should return promptly after pipe close.

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        _join_threads()
        _kill_docker_container(command)

        cmd_str = _mask_url_credentials(shlex.join(command))

        def _tail(buf: bytearray) -> str:
            # Note: because _read_stream_bounded caps capture at MAX_OUTPUT_SIZE_BYTES+1,
            # this tail reflects the end of the *captured prefix*, not necessarily the
            # true end of the process output if it produced more than the limit.
            tail_bytes = buf[-1024:] if len(buf) > 1024 else buf
            tail = tail_bytes.decode("utf-8", errors="replace").strip()
            return f"... (tail of captured prefix)\n{tail}" if len(buf) > 1024 else tail

        stdout_tail = _tail(stdout_buf)
        stderr_tail = _tail(stderr_buf)
        output = (
            f"stderr: {stderr_tail}\nstdout: {stdout_tail}".strip()
            if stderr_tail or stdout_tail
            else "command timed out without output"
        )

        raise DockerSandboxRunnerError(
            f"Docker sandbox command timed out after {timeout}s ({cmd_str}): {output}"
        ) from exc
    finally:
        # Always join threads and clean up pipes, even on unexpected exceptions
        # such as KeyboardInterrupt or SystemExit.
        _join_threads()

    if limit_exceeded.is_set():
        _kill_docker_container(command)
        cmd_str = _mask_url_credentials(shlex.join(command))
        stdout_str = _decode_bounded(stdout_buf, MAX_OUTPUT_SIZE_BYTES)
        stderr_str = _decode_bounded(stderr_buf, MAX_OUTPUT_SIZE_BYTES)
        msg = (
            f"Docker sandbox output limit exceeded ({MAX_OUTPUT_SIZE_BYTES} bytes) "
            f"for command ({cmd_str}). Partial output:\n"
            f"stderr: {stderr_str}\nstdout: {stdout_str}"
        )
        raise DockerSandboxOutputLimitError(msg)

    stdout_str = _decode_bounded(stdout_buf, MAX_OUTPUT_SIZE_BYTES)
    stderr_str = _decode_bounded(stderr_buf, MAX_OUTPUT_SIZE_BYTES)

    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode,
        stdout=stdout_str,
        stderr=stderr_str,
    )


class DockerSandboxRunner:
    """Run commands inside a docker container with a mounted task workspace."""

    def __init__(
        self,
        *,
        default_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        command_runner: DockerCommandRunner | None = None,
    ) -> None:
        self.default_image = default_image
        self.docker_binary = docker_binary
        self._command_runner = command_runner or _run_docker_command

    def run(self, request: DockerSandboxCommand) -> DockerSandboxResult:
        """Run the requested command in a sandboxed container."""
        image = request.image or self.default_image
        docker_command = _build_docker_run_command(
            request,
            image=image,
            docker_binary=self.docker_binary,
        )

        logger.info(
            "Running docker sandbox command",
            extra={
                "workspace_id": request.workspace.workspace_id,
                "task_id": request.workspace.task_id,
                "image": image,
                "command": [_mask_url_credentials(arg) for arg in request.command],
            },
        )

        started_at = perf_counter()
        completed = self._command_runner(
            docker_command,
            timeout=request.timeout_seconds,
        )
        duration_seconds = perf_counter() - started_at

        result = DockerSandboxResult(
            image=image,
            command=request.command,
            docker_command=docker_command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=duration_seconds,
        )

        logger.info(
            "Docker sandbox command finished",
            extra={
                "workspace_id": request.workspace.workspace_id,
                "task_id": request.workspace.task_id,
                "image": image,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            },
        )
        return result
