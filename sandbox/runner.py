"""Docker-based command runner for sandbox workspaces."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import typing
from pathlib import Path
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from pydantic import Field

from sandbox.container import build_container_name
from sandbox.streams import MAX_OUTPUT_SIZE_BYTES, decode_bounded, read_stream_bounded
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
    files_changed: list[str] = Field(default_factory=list)
    artifacts: list[SandboxArtifact] = Field(default_factory=list)


class SandboxArtifact(SandboxModel):
    """A persisted artifact produced by a sandbox command run."""

    name: str
    uri: str
    artifact_type: str | None = None
    artifact_metadata: dict[str, typing.Any] = Field(default_factory=dict)


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

_ARTIFACT_ROOT_DIR = "artifacts"
_ARTIFACT_RUN_DIR_PREFIX = "command-"


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

    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode,
        stdout=stdout_str,
        stderr=stderr_str,
    )


def _run_git_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    """Run a git inspection command against the workspace repo."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        timeout=timeout,
    )


def _artifact_run_directory(workspace: WorkspaceHandle) -> Path:
    """Create a unique artifact directory for one sandbox command result."""
    artifact_dir = workspace.workspace_path / _ARTIFACT_ROOT_DIR
    artifact_dir = artifact_dir / f"{_ARTIFACT_RUN_DIR_PREFIX}{uuid4().hex[:8]}"
    artifact_dir.mkdir(parents=True, exist_ok=False)
    return artifact_dir


def _write_text_artifact(
    workspace: WorkspaceHandle,
    artifact_dir: Path,
    *,
    filename: str,
    content: str,
    artifact_type: str,
    artifact_metadata: dict[str, typing.Any] | None = None,
) -> SandboxArtifact:
    """Persist a text artifact under the workspace artifact directory."""
    artifact_path = artifact_dir / filename
    artifact_path.write_text(content, encoding="utf-8")
    return SandboxArtifact(
        name=filename,
        uri=str(artifact_path.relative_to(workspace.workspace_path)),
        artifact_type=artifact_type,
        artifact_metadata=artifact_metadata or {},
    )


def _parse_git_status_entries(status_output: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain=v1 -z` output into `(status, path)` tuples."""
    entries: list[tuple[str, str]] = []
    tokens = iter(status_output.split("\0"))
    for token in tokens:
        if not token:
            continue

        status = token[:2]
        path = token[3:]

        if "R" in status or "C" in status:
            new_path = next(tokens, "")
            if new_path:
                path = new_path

        entries.append((status, path))

    return entries


def _format_changed_files(files_changed: list[str]) -> str:
    """Render the changed-file snapshot as a readable text artifact."""
    if not files_changed:
        return "No changed files detected.\n"
    return "".join(f"{path}\n" for path in files_changed)


def _build_diff_summary(
    repo_path: Path,
    *,
    untracked_files: list[str],
) -> str | None:
    """Build an optional diff summary artifact for tracked and untracked changes."""
    sections: list[str] = []
    head_check = _run_git_command(["git", "rev-parse", "--verify", "HEAD"], cwd=repo_path)
    if head_check.returncode == 0:
        tracked_diff = _run_git_command(
            ["git", "diff", "--stat", "--summary", "HEAD", "--"],
            cwd=repo_path,
        )
        if tracked_diff.returncode == 0:
            tracked_summary = tracked_diff.stdout.decode("utf-8", errors="replace").strip()
            if tracked_summary:
                sections.append(tracked_summary)
        else:
            logger.warning(
                "Failed to collect sandbox diff summary",
                extra={
                    "repo_path": str(repo_path),
                    "stderr": tracked_diff.stderr.decode("utf-8", errors="replace").strip(),
                },
            )

    if untracked_files:
        sections.append(
            "Untracked files:\n" + "".join(f"- {path}\n" for path in untracked_files).rstrip()
        )

    summary = "\n\n".join(section for section in sections if section).strip()
    return summary or None


def _capture_artifacts(
    request: DockerSandboxCommand,
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> tuple[list[str], list[SandboxArtifact]]:
    """Persist command artifacts and snapshot workspace changes after a sandbox run."""
    artifact_dir = _artifact_run_directory(request.workspace)
    artifacts = [
        _write_text_artifact(
            request.workspace,
            artifact_dir,
            filename="stdout.log",
            content=stdout,
            artifact_type="log",
            artifact_metadata={"stream": "stdout", "exit_code": exit_code},
        ),
        _write_text_artifact(
            request.workspace,
            artifact_dir,
            filename="stderr.log",
            content=stderr,
            artifact_type="log",
            artifact_metadata={"stream": "stderr", "exit_code": exit_code},
        ),
    ]

    files_changed: list[str] = []
    try:
        status_result = _run_git_command(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=request.workspace.repo_path,
        )
        if status_result.returncode != 0:
            raise DockerSandboxRunnerError(
                status_result.stderr.decode("utf-8", errors="replace").strip()
                or "git status failed without output"
            )

        status_entries = _parse_git_status_entries(
            status_result.stdout.decode("utf-8", errors="replace")
        )
        files_changed = list(dict.fromkeys(path for _status, path in status_entries))
        untracked_files = [path for status, path in status_entries if status == "??"]
        artifacts.append(
            _write_text_artifact(
                request.workspace,
                artifact_dir,
                filename="changed-files.txt",
                content=_format_changed_files(files_changed),
                artifact_type="result_summary",
                artifact_metadata={
                    "kind": "changed_files",
                    "files_changed_count": len(files_changed),
                },
            )
        )

        diff_summary = _build_diff_summary(
            request.workspace.repo_path,
            untracked_files=untracked_files,
        )
        if diff_summary:
            artifacts.append(
                _write_text_artifact(
                    request.workspace,
                    artifact_dir,
                    filename="diff-summary.txt",
                    content=diff_summary + "\n",
                    artifact_type="diff",
                    artifact_metadata={
                        "kind": "diff_summary",
                        "files_changed_count": len(files_changed),
                    },
                )
            )
    except (DockerSandboxRunnerError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "Failed to inspect sandbox workspace changes",
            extra={
                "workspace_id": request.workspace.workspace_id,
                "task_id": request.workspace.task_id,
                "error": str(exc),
            },
        )
        artifacts.append(
            _write_text_artifact(
                request.workspace,
                artifact_dir,
                filename="changed-files.txt",
                content=f"Failed to inspect workspace changes: {exc}\n",
                artifact_type="result_summary",
                artifact_metadata={"kind": "changed_files", "inspection_error": str(exc)},
            )
        )

    return files_changed, artifacts


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
        files_changed, artifacts = _capture_artifacts(
            request,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
        )

        result = DockerSandboxResult(
            image=image,
            command=request.command,
            docker_command=docker_command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
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
