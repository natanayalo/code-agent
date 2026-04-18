"""Long-lived Docker container helpers for persistent sandbox sessions."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Protocol

from pydantic import Field

from sandbox.workspace import SandboxModel, WorkspaceHandle, _mask_url_credentials

logger = logging.getLogger(__name__)


class DockerContainerCommandRunner(Protocol):
    """Protocol for running docker commands on the host."""

    def __call__(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]: ...


class DockerSandboxContainerRequest(SandboxModel):
    """A request to start a long-lived Docker container for a workspace."""

    workspace: WorkspaceHandle
    image: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    working_dir: str = "/workspace/repo"
    network_enabled: bool = False
    memory_limit: str | None = "1g"
    cpu_limit: float | None = 1.0
    keepalive_command: list[str] = Field(
        default_factory=lambda: ["sleep", "infinity"],
        min_length=1,
    )
    start_timeout_seconds: int = Field(default=30, ge=1)


class DockerSandboxContainer(SandboxModel):
    """Handle for a running long-lived sandbox container."""

    workspace: WorkspaceHandle
    container_name: str
    image: str
    working_dir: str = "/workspace/repo"
    environment: dict[str, str] = Field(default_factory=dict)
    network_enabled: bool = False
    memory_limit: str | None = "1g"
    cpu_limit: float | None = 1.0


class DockerSandboxContainerError(RuntimeError):
    """Raised when a persistent sandbox container cannot be managed."""


def build_container_name(workspace: WorkspaceHandle) -> str:
    """Build a deterministic docker container name for a workspace."""
    return f"sandbox-{workspace.workspace_id}"


def _build_docker_container_run_command(
    request: DockerSandboxContainerRequest,
    *,
    image: str,
    docker_binary: str = "docker",
) -> list[str]:
    """Build the `docker run -d` command for a persistent sandbox container."""
    container_name = build_container_name(request.workspace)
    command = [
        docker_binary,
        "run",
        "-d",
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
        raise DockerSandboxContainerError(
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
        pass

    if not request.network_enabled:
        command.extend(["--network", "none"])
    for key, value in sorted(request.environment.items()):
        command.extend(["--env", f"{key}={value}"])
    command.append(image)
    command.extend(request.keepalive_command)
    return command


def _build_docker_container_remove_command(
    container_name: str,
    *,
    docker_binary: str = "docker",
) -> list[str]:
    """Build the `docker rm -f` command for a persistent sandbox container."""
    return [docker_binary, "rm", "-f", container_name]


def _build_docker_container_inspect_command(
    container_name: str,
    *,
    docker_binary: str = "docker",
) -> list[str]:
    """Build the `docker inspect` command used to verify a running container."""
    return [docker_binary, "inspect", "--format", "{{.State.Running}}", container_name]


def _run_docker_command(
    command: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run a Docker CLI command and raise a container-specific error on OS failures."""
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except OSError as exc:
        cmd_str = _mask_url_credentials(shlex.join(command))
        raise DockerSandboxContainerError(
            f"Failed to start Docker container command ({cmd_str}): {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        cmd_str = _mask_url_credentials(shlex.join(command))
        raise DockerSandboxContainerError(
            f"Docker container command timed out after {timeout}s ({cmd_str})"
        ) from exc


class DockerSandboxContainerManager:
    """Manage the lifecycle of named long-lived Docker containers."""

    def __init__(
        self,
        *,
        default_image: str = "python:3.12-slim",
        docker_binary: str = "docker",
        command_runner: DockerContainerCommandRunner | None = None,
        inspect_timeout_seconds: int = 10,
        stop_timeout_seconds: int = 15,
    ) -> None:
        self.default_image = default_image
        self.docker_binary = docker_binary
        self._command_runner = command_runner or _run_docker_command
        self.inspect_timeout_seconds = inspect_timeout_seconds
        self.stop_timeout_seconds = stop_timeout_seconds

    def start(self, request: DockerSandboxContainerRequest) -> DockerSandboxContainer:
        """Start a named long-lived Docker container for a sandbox workspace."""
        image = request.image or self.default_image
        docker_command = _build_docker_container_run_command(
            request,
            image=image,
            docker_binary=self.docker_binary,
        )

        logger.info(
            "Starting persistent sandbox container",
            extra={
                "workspace_id": request.workspace.workspace_id,
                "task_id": request.workspace.task_id,
                "container_name": build_container_name(request.workspace),
                "image": image,
            },
        )
        completed = self._command_runner(
            docker_command,
            timeout=request.start_timeout_seconds,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            message = stderr or stdout or "docker run failed without output"
            raise DockerSandboxContainerError(
                f"Failed to start persistent sandbox container "
                f"({build_container_name(request.workspace)}): {message}"
            )

        return DockerSandboxContainer(
            workspace=request.workspace,
            container_name=build_container_name(request.workspace),
            image=image,
            working_dir=request.working_dir,
            environment=dict(request.environment),
            network_enabled=request.network_enabled,
            memory_limit=request.memory_limit,
            cpu_limit=request.cpu_limit,
        )

    def reconnect(self, container: DockerSandboxContainer) -> DockerSandboxContainer:
        """Verify that a named persistent container is still running and reusable."""
        inspect_command = _build_docker_container_inspect_command(
            container.container_name,
            docker_binary=self.docker_binary,
        )
        completed = self._command_runner(
            inspect_command,
            timeout=self.inspect_timeout_seconds,
        )
        if completed.returncode != 0 or completed.stdout.strip() != "true":
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            message = stderr or stdout or "container is not running"
            raise DockerSandboxContainerError(
                f"Failed to reconnect to sandbox container {container.container_name}: {message}"
            )
        return container

    def stop(self, container: DockerSandboxContainer) -> None:
        """Force-stop and remove a persistent sandbox container."""
        remove_command = _build_docker_container_remove_command(
            container.container_name,
            docker_binary=self.docker_binary,
        )
        logger.info(
            "Stopping persistent sandbox container",
            extra={
                "workspace_id": container.workspace.workspace_id,
                "task_id": container.workspace.task_id,
                "container_name": container.container_name,
            },
        )
        completed = self._command_runner(
            remove_command,
            timeout=self.stop_timeout_seconds,
        )
        if completed.returncode == 0:
            return

        message = (completed.stderr.strip() or completed.stdout.strip()).lower()
        if "no such container" in message:
            return

        raise DockerSandboxContainerError(
            f"Failed to stop sandbox container {container.container_name}: "
            f"{completed.stderr.strip() or completed.stdout.strip() or 'docker rm failed'}"
        )
