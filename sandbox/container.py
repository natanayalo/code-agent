"""Long-lived Docker container helpers for persistent sandbox sessions."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import url2pathname

from pydantic import Field

from sandbox.redact import mask_url_credentials as _mask_url_credentials
from sandbox.workspace import SandboxModel, WorkspaceHandle

logger = logging.getLogger(__name__)

DEFAULT_SANDBOX_IMAGE = "python:3.12-slim"


class DockerContainerCommandRunner(Protocol):
    """Protocol for running docker commands on the host."""

    def __call__(self, command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]: ...


class DockerSandboxContainerRequest(SandboxModel):
    """Request to start a persistent sandbox container."""

    workspace: WorkspaceHandle
    image: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    working_dir: str | None = None
    network_enabled: bool = False
    memory_limit: str | None = "1g"
    cpu_limit: float | None = 1.0
    keepalive_command: list[str] = Field(
        default_factory=lambda: ["sleep", "infinity"],
        min_length=1,
    )
    start_timeout_seconds: int = Field(default=30, ge=1)
    read_only_workspace: bool = False


class DockerSandboxContainer(SandboxModel):
    """Handle for a running long-lived sandbox container."""

    workspace: WorkspaceHandle
    container_name: str
    image: str
    working_dir: str
    environment: dict[str, str] = Field(default_factory=dict)
    network_enabled: bool = False
    memory_limit: str | None = "1g"
    cpu_limit: float | None = 1.0


class DockerSandboxContainerError(RuntimeError):
    """Raised when a persistent sandbox container cannot be managed."""


def build_container_name(workspace: WorkspaceHandle) -> str:
    """Build a deterministic docker container name for a workspace."""
    return f"sandbox-{workspace.workspace_id}"


def _append_resource_limits(command: list[str], request: DockerSandboxContainerRequest) -> None:
    if request.memory_limit:
        command.extend(["--memory", request.memory_limit])
    if request.cpu_limit:
        command.extend(["--cpus", str(request.cpu_limit)])


def append_workspace_mount_options(
    command: list[str],
    workspace_path: Path,
    working_dir: str | None,
    read_only_workspace: bool,
) -> tuple[Path, str]:
    target_path = str(workspace_path)
    if working_dir is None or working_dir == "/workspace":
        working_dir = target_path

    ro_flag = ",readonly" if read_only_workspace else ""
    command.extend(
        [
            "--workdir",
            working_dir,
            "--mount",
            f"type=bind,source={workspace_path},target={target_path}{ro_flag}",
        ]
    )

    if read_only_workspace:
        code_agent_path = workspace_path / ".code-agent"
        code_agent_path.mkdir(parents=True, exist_ok=True)
        agent_home_path = workspace_path / ".agent_home"
        agent_home_path.mkdir(parents=True, exist_ok=True)
        artifacts_path = workspace_path / "artifacts"
        artifacts_path.mkdir(parents=True, exist_ok=True)
        sandbox_db_path = code_agent_path / ".sandbox.db"

        symlink_path = workspace_path / ".sandbox.db"
        if symlink_path.exists() or symlink_path.is_symlink():
            try:
                if symlink_path.is_symlink():
                    target = os.readlink(symlink_path)
                    if Path(target).as_posix() != ".code-agent/.sandbox.db":
                        symlink_path.unlink()
                else:
                    if symlink_path.is_file() and not sandbox_db_path.exists():
                        symlink_path.rename(sandbox_db_path)
                    else:
                        symlink_path.unlink()
            except OSError:
                pass
        if not symlink_path.exists() and not symlink_path.is_symlink():
            try:
                symlink_path.symlink_to(".code-agent/.sandbox.db")
            except OSError:
                pass

        sandbox_db_path.touch(exist_ok=True)

        command.extend(
            ["--mount", f"type=bind,source={code_agent_path},target={target_path}/.code-agent"]
        )
        command.extend(
            ["--mount", f"type=bind,source={agent_home_path},target={target_path}/.agent_home"]
        )
        command.extend(
            ["--mount", f"type=bind,source={artifacts_path},target={target_path}/artifacts"]
        )

    return workspace_path, working_dir


def _append_workspace_mount(
    command: list[str], request: DockerSandboxContainerRequest
) -> tuple[Path, str]:
    workspace_path = request.workspace.workspace_path.resolve()
    if "," in str(workspace_path):
        raise DockerSandboxContainerError(
            f"Workspace path contains a comma which is incompatible with "
            f"the --mount syntax: {workspace_path}"
        )
    return append_workspace_mount_options(
        command=command,
        workspace_path=workspace_path,
        working_dir=request.working_dir,
        read_only_workspace=request.read_only_workspace,
    )


def _local_repo_path_from_file_url(repo_url: str) -> str | None:
    parsed_url = urlparse(repo_url)
    if parsed_url.scheme != "file":
        return None
    if not parsed_url.path:
        raise DockerSandboxContainerError("Local repo path cannot be empty for file:// scheme")
    return os.path.abspath(url2pathname(parsed_url.path))


def _is_allowed_local_remote(resolved_path: Path) -> bool:
    allowed_remotes_env = os.environ.get("CODE_AGENT_ALLOWED_LOCAL_REMOTES", "")
    for path_text in allowed_remotes_env.split(","):
        path_text = path_text.strip()
        if path_text and resolved_path.is_relative_to(Path(path_text).resolve()):
            return True
    return False


def _raise_if_sibling_workspace(
    *,
    resolved_path: Path,
    workspace_path: Path,
    allowed_root: Path,
) -> None:
    is_sibling = not resolved_path.is_relative_to(workspace_path)
    if resolved_path == workspace_path or not is_sibling:
        return

    rel_parts = resolved_path.relative_to(allowed_root).parts
    if rel_parts and rel_parts[0].startswith("workspace-"):
        raise DockerSandboxContainerError(
            f"Mounting sibling workspaces is forbidden: {resolved_path}"
        )


def _validate_local_repo_mount_path(local_repo_path: str, workspace_path: Path) -> None:
    from sandbox.workspace import default_workspace_root

    resolved_path = Path(local_repo_path).resolve()
    allowed_root = default_workspace_root().resolve()

    if resolved_path == allowed_root:
        raise DockerSandboxContainerError(
            f"Mounting the workspace root {allowed_root} is forbidden"
        )

    if resolved_path.is_relative_to(allowed_root):
        _raise_if_sibling_workspace(
            resolved_path=resolved_path,
            workspace_path=workspace_path,
            allowed_root=allowed_root,
        )
        return

    if not _is_allowed_local_remote(resolved_path):
        raise DockerSandboxContainerError(
            f"Local repo path {resolved_path} is outside the allowed workspace root {allowed_root}"
        )


def _append_local_repo_mount(
    command: list[str], request: DockerSandboxContainerRequest, workspace_path: Path
) -> None:
    if not request.workspace.repo_url:
        return

    local_repo_path = _local_repo_path_from_file_url(request.workspace.repo_url)
    if local_repo_path is None:
        return

    _validate_local_repo_mount_path(local_repo_path, workspace_path)
    if "," in local_repo_path:
        raise DockerSandboxContainerError(
            "Local repo path contains a comma which is incompatible with "
            f"the --mount syntax: {local_repo_path}"
        )
    ro_flag = ",readonly" if request.read_only_workspace else ""
    command.extend(
        ["--mount", f"type=bind,source={local_repo_path},target={local_repo_path}{ro_flag}"]
    )


def _append_user_options(command: list[str]) -> None:
    try:
        uid = os.getuid()
        gid = os.getgid()
        command.extend(["--user", f"{uid}:{gid}"])
    except AttributeError:
        pass


def _append_environment_options(
    command: list[str], request: DockerSandboxContainerRequest, working_dir: str
) -> None:
    # T-182: Ensure a writable HOME for tools like poetry/npm
    effective_env = dict(request.environment)
    if "HOME" not in effective_env:
        effective_env["HOME"] = working_dir

    for key, value in sorted(effective_env.items()):
        command.extend(["--env", f"{key}={value}"])


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
    _append_resource_limits(command, request)

    workspace_path, working_dir = _append_workspace_mount(command, request)
    _append_local_repo_mount(command, request, workspace_path)
    _append_user_options(command)
    if not request.network_enabled:
        command.extend(["--network", "none"])

    _append_environment_options(command, request, working_dir)
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
        default_image: str = DEFAULT_SANDBOX_IMAGE,
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

        working_dir = request.working_dir
        if working_dir is None or working_dir == "/workspace":
            working_dir = str(request.workspace.workspace_path.resolve())

        return DockerSandboxContainer(
            workspace=request.workspace,
            container_name=build_container_name(request.workspace),
            image=image,
            working_dir=working_dir,
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
