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


def _read_stream_bounded(stream: typing.IO[bytes], limit: int) -> bytearray:
    """Read from *stream* into a bytearray, discarding bytes beyond *limit*.

    The stream is drained to its end so the subprocess pipe never blocks,
    regardless of how much data the container produces.
    """
    buf = bytearray()
    for chunk in iter(lambda: stream.read(65536), b""):
        remaining = (limit + 1) - len(buf)
        if remaining > 0:
            buf.extend(chunk[:remaining])
        # Keep draining even after limit is reached so the pipe never blocks.
    return buf


def _decode_bounded(buf: bytearray, limit: int) -> str:
    """Decode *buf* to a string, appending a truncation marker when needed."""
    text = buf[:limit].decode("utf-8", errors="replace")
    if len(buf) > limit:
        text += "\n... (truncated)"
    return text


def _build_docker_run_command(
    request: DockerSandboxCommand,
    *,
    image: str,
    docker_binary: str = "docker",
) -> list[str]:
    """Build a deterministic docker run command for the sandbox request."""
    command = [
        docker_binary,
        "run",
        "--rm",
    ]
    if request.memory_limit:
        command.extend(["--memory", request.memory_limit])
    if request.cpu_limit:
        command.extend(["--cpus", str(request.cpu_limit)])
    command.extend(
        [
            "--workdir",
            request.working_dir,
            "--mount",
            f"type=bind,source={request.workspace.workspace_path.resolve()},target=/workspace",
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

    stdout_thread = threading.Thread(
        target=lambda: stdout_buf.extend(
            _read_stream_bounded(proc.stdout, MAX_OUTPUT_SIZE_BYTES)  # type: ignore[arg-type]
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=lambda: stderr_buf.extend(
            _read_stream_bounded(proc.stderr, MAX_OUTPUT_SIZE_BYTES)  # type: ignore[arg-type]
        ),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        stdout_thread.join()
        stderr_thread.join()

        cmd_str = _mask_url_credentials(shlex.join(command))

        def _tail(buf: bytearray) -> str:
            tail_bytes = buf[-1024:] if len(buf) > 1024 else buf
            tail = tail_bytes.decode("utf-8", errors="replace").strip()
            return f"... (truncated)\n{tail}" if len(buf) > 1024 else tail

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

    stdout_thread.join()
    stderr_thread.join()

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
