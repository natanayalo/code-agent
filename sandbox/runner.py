"""Docker-based command runner for sandbox workspaces."""

from __future__ import annotations

import logging
import shlex
import subprocess
import tempfile
import typing
from time import perf_counter
from typing import Protocol

from pydantic import Field

from sandbox.workspace import SandboxModel, WorkspaceHandle, _mask_url_credentials

logger = logging.getLogger(__name__)


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
            f"type=bind,source='{request.workspace.workspace_path.resolve()}',target=/workspace",
        ]
    )

    try:
        import os

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
    """Run docker and safely capture stdout/stderr up to a limit."""
    limit = 2 * 1024 * 1024  # 2MB

    # We use temporary files to capture unbounded output safely without host OOM.
    with (
        tempfile.TemporaryFile(mode="w+b") as out,
        tempfile.TemporaryFile(mode="w+b") as err,
    ):
        try:
            proc = subprocess.run(
                command,
                check=False,
                stdout=out,
                stderr=err,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            cmd_str = _mask_url_credentials(shlex.join(command))

            def _read_tail(f: typing.IO[bytes]) -> str:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 1024))
                tail = f.read().decode("utf-8", errors="replace").strip()
                if size > 1024:
                    return f"... (truncated)\n{tail}"
                return tail

            stdout = _read_tail(out)
            stderr = _read_tail(err)

            output = stderr or stdout or "command timed out without output"

            raise DockerSandboxRunnerError(
                f"Docker sandbox command timed out after {timeout}s ({cmd_str}): {output}"
            ) from exc
        except OSError as exc:
            cmd_str = _mask_url_credentials(shlex.join(command))
            raise DockerSandboxRunnerError(
                f"Failed to start Docker sandbox command ({cmd_str}): {exc}"
            ) from exc

        out.seek(0)
        err.seek(0)

        stdout_bytes = out.read(limit + 1)
        if len(stdout_bytes) > limit:
            stdout_str = (
                stdout_bytes[:limit].decode("utf-8", errors="replace") + "\n... (truncated)"
            )
        else:
            stdout_str = stdout_bytes.decode("utf-8", errors="replace")

        stderr_bytes = err.read(limit + 1)
        if len(stderr_bytes) > limit:
            stderr_str = (
                stderr_bytes[:limit].decode("utf-8", errors="replace") + "\n... (truncated)"
            )
        else:
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")

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
