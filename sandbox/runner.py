"""Docker-based command runner for sandbox workspaces."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
from time import perf_counter
from typing import Protocol

from pydantic import Field

from apps.observability import capture_trace_context
from sandbox.audit import capture_audit_artifacts
from sandbox.container import build_container_name
from sandbox.policy import PathPolicy
from sandbox.redact import SecretRedactor
from sandbox.streams import MAX_OUTPUT_SIZE_BYTES, decode_bounded, read_stream_bounded
from sandbox.workspace import (
    SandboxArtifact,
    SandboxModel,
    WorkspaceHandle,
    _mask_url_credentials,
)

logger = logging.getLogger(__name__)


class DockerCommandRunner(Protocol):
    """Protocol for running docker commands on the host."""

    def __call__(
        self,
        command: list[str],
        *,
        timeout: int,
        redactor: SecretRedactor | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


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
    secrets: dict[str, str] = Field(default_factory=dict)
    path_policy: PathPolicy | None = None


class DockerSandboxResult(SandboxModel):
    """Captured result of a Docker sandbox command."""

    image: str
    command: list[str]
    docker_command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)
    files_changed: list[str] = Field(default_factory=list)
    artifacts: list[SandboxArtifact] = Field(default_factory=list)


class DockerSandboxRunnerError(RuntimeError):
    """Raised when the Docker sandbox cannot start or complete."""


class DockerSandboxOutputLimitError(DockerSandboxRunnerError):
    """Raised when the sandbox exceeds capture limits."""


def _build_container_name(workspace: WorkspaceHandle) -> str:
    """Build a deterministic docker container name for a workspace run."""
    return build_container_name(workspace)


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


_read_stream_bounded = read_stream_bounded


_decode_bounded = decode_bounded


def _build_docker_run_command(
    request: DockerSandboxCommand,
    *,
    image: str,
    docker_binary: str = "docker",
) -> list[str]:
    if request.path_policy:
        if not request.path_policy.check_path(request.working_dir):
            raise DockerSandboxRunnerError(
                f"Working directory is denied by path policy: {request.working_dir}"
            )

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

    # Inject TRACEPARENT from the current context if available, enabling
    # unified tracing across service/sandbox boundaries.
    effective_env = dict(request.environment)
    trace_context = capture_trace_context()
    if "traceparent" in trace_context:
        effective_env.setdefault("TRACEPARENT", trace_context["traceparent"])

    for key, value in sorted(effective_env.items()):
        command.extend(["--env", f"{key}={value}"])
    command.append(image)
    command.extend(request.command)
    return command


# Seconds to wait for reader threads to finish after the process exits.
# If a child process inside the container keeps pipes open, we close them
# explicitly after this window to unblock the threads.
_THREAD_JOIN_TIMEOUT: float = 5.0

_ARTIFACT_ROOT_DIR = "artifacts"
_ARTIFACT_RUN_DIR_PREFIX = "command-"


def _run_docker_command(
    command: list[str],
    *,
    timeout: int,
    redactor: SecretRedactor | None = None,
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

    stdout_pipe = proc.stdout
    stderr_pipe = proc.stderr
    assert stdout_pipe is not None  # noqa: S101 – guaranteed by PIPE
    assert stderr_pipe is not None  # noqa: S101 – guaranteed by PIPE

    limit_exceeded = threading.Event()

    def kill_on_limit() -> None:
        limit_exceeded.set()
        proc.kill()

    stdout_thread = threading.Thread(
        target=lambda: stdout_buf.extend(
            _read_stream_bounded(
                stdout_pipe,
                MAX_OUTPUT_SIZE_BYTES,
                on_limit=kill_on_limit,
            )
        ),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=lambda: stderr_buf.extend(
            _read_stream_bounded(
                stderr_pipe,
                MAX_OUTPUT_SIZE_BYTES,
                on_limit=kill_on_limit,
            )
        ),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    def _join_threads() -> None:
        """Join reader threads, closing pipes if they hang past the timeout."""
        for thread, pipe in [
            (stdout_thread, stdout_pipe),
            (stderr_thread, stderr_pipe),
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

    if redactor:
        stdout_str = redactor.redact(stdout_str)
        stderr_str = redactor.redact(stderr_str)

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

        redactor = SecretRedactor(list(request.secrets.values())) if request.secrets else None

        logger.info(
            "Running docker sandbox command",
            extra={
                "workspace_id": request.workspace.workspace_id,
                "task_id": request.workspace.task_id,
                "image": image,
                "command": [
                    redactor.redact(_mask_url_credentials(arg))
                    if redactor
                    else _mask_url_credentials(arg)
                    for arg in request.command
                ],
            },
        )

        started_at = perf_counter()
        completed = self._command_runner(
            docker_command,
            timeout=request.timeout_seconds,
            redactor=redactor,
        )
        duration_seconds = perf_counter() - started_at

        redacted_stdout = redactor.redact(completed.stdout) if redactor else completed.stdout
        redacted_stderr = redactor.redact(completed.stderr) if redactor else completed.stderr

        files_changed, artifacts = capture_audit_artifacts(
            request.workspace,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            redactor=redactor,
        )

        result = DockerSandboxResult(
            image=image,
            command=request.command,
            docker_command=docker_command,
            exit_code=completed.returncode,
            stdout=redacted_stdout,
            stderr=redacted_stderr,
            duration_seconds=duration_seconds,
            files_changed=files_changed,
            artifacts=artifacts,
        )

        logger.info(
            "Docker sandbox command finished",
            extra={
                "workspace_id": request.workspace.workspace_id,
                "task_id": request.workspace.task_id,
                "image": image,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "files_changed_count": len(result.files_changed),
                "artifact_count": len(result.artifacts),
            },
        )
        return result
