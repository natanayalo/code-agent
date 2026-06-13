"""Unit tests for persistent sandbox container lifecycle helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sandbox.container import (
    DockerSandboxContainer,
    DockerSandboxContainerError,
    DockerSandboxContainerManager,
    DockerSandboxContainerRequest,
    _build_docker_container_inspect_command,
    _build_docker_container_remove_command,
    _build_docker_container_run_command,
    _run_docker_command,
    build_container_name,
)
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle


def _workspace_handle(tmp_path: Path) -> WorkspaceHandle:
    workspace_path = tmp_path / "workspace-task-45"
    repo_path = workspace_path
    repo_path.mkdir(parents=True)
    return WorkspaceHandle(
        workspace_id="workspace-task-45",
        task_id="task-45",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


def test_build_docker_container_run_command_detaches_and_mounts_workspace(tmp_path: Path) -> None:
    """Persistent containers should run detached with the workspace bind mount."""
    request = DockerSandboxContainerRequest(
        workspace=_workspace_handle(tmp_path),
        environment={"PYTHONUNBUFFERED": "1"},
    )

    command = _build_docker_container_run_command(request, image="python:3.12-slim")

    expected_prefix = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        build_container_name(request.workspace),
        "--memory",
        "1g",
        "--cpus",
        "1.0",
        "--workdir",
        str(request.workspace.workspace_path.resolve()),
        "--mount",
        f"type=bind,source={request.workspace.workspace_path.resolve()},target={request.workspace.workspace_path.resolve()}",
    ]
    assert command[: len(expected_prefix)] == expected_prefix
    assert command[-3:] == ["python:3.12-slim", "sleep", "infinity"]
    assert "--network" in command
    assert "--env" in command


def test_build_docker_container_run_command_raises_on_comma_in_path(tmp_path: Path) -> None:
    """Workspace paths containing commas should fail fast before docker run."""
    workspace_path = tmp_path / "work,space"
    repo_path = workspace_path
    repo_path.mkdir(parents=True)
    request = DockerSandboxContainerRequest(
        workspace=WorkspaceHandle(
            workspace_id="workspace-task-45",
            task_id="task-45",
            workspace_path=workspace_path,
            repo_path=repo_path,
            repo_url="https://example.com/repo.git",
            cleanup_policy=WorkspaceCleanupPolicy(),
        )
    )

    with pytest.raises(DockerSandboxContainerError, match="Workspace path contains a comma"):
        _build_docker_container_run_command(request, image="python:3.12-slim")


def test_build_docker_container_run_command_skips_user_mapping_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If os.getuid/getgid are unavailable we should omit docker user mapping."""
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.delattr(os, "getgid", raising=False)
    request = DockerSandboxContainerRequest(workspace=_workspace_handle(tmp_path))

    command = _build_docker_container_run_command(request, image="python:3.12-slim")

    assert "--user" not in command


def test_container_manager_start_reconnect_and_stop(tmp_path: Path) -> None:
    """Container lifecycle commands should be issued in the expected order."""
    workspace = _workspace_handle(tmp_path)
    calls: list[tuple[list[str], int]] = []

    def fake_runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append((command, timeout))
        if command[1:3] == ["run", "-d"]:
            return subprocess.CompletedProcess(command, 0, stdout="container-id\n", stderr="")
        if command[1] == "inspect":
            return subprocess.CompletedProcess(command, 0, stdout="true\n", stderr="")
        if command[1] == "rm":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected docker command: {command}")

    manager = DockerSandboxContainerManager(command_runner=fake_runner)
    request = DockerSandboxContainerRequest(workspace=workspace)

    container = manager.start(request)
    reconnected = manager.reconnect(container)
    manager.stop(container)

    assert container == reconnected
    assert calls == [
        (
            _build_docker_container_run_command(
                request,
                image="python:3.12-slim",
            ),
            30,
        ),
        (
            _build_docker_container_inspect_command(container.container_name),
            10,
        ),
        (
            _build_docker_container_remove_command(container.container_name),
            15,
        ),
    ]


def test_container_manager_reconnect_raises_for_stopped_container(tmp_path: Path) -> None:
    """Reconnect should fail fast when the named container is no longer running."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name=build_container_name(workspace),
        image="python:3.12-slim",
        working_dir=str(workspace.workspace_path.resolve()),
    )

    def fake_runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="No such container")

    manager = DockerSandboxContainerManager(command_runner=fake_runner)

    with pytest.raises(DockerSandboxContainerError, match="Failed to reconnect"):
        manager.reconnect(container)


def test_run_docker_command_raises_on_os_error() -> None:
    """OS failures while spawning docker should surface as container errors."""
    with patch("subprocess.run", side_effect=OSError("docker missing")):
        with pytest.raises(
            DockerSandboxContainerError,
            match=r"Failed to start Docker container command .* docker missing",
        ):
            _run_docker_command(["docker", "run", "-d"], timeout=30)


def test_run_docker_command_raises_on_timeout() -> None:
    """Timed-out docker commands should surface as container errors."""
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker run -d", timeout=30),
    ):
        with pytest.raises(
            DockerSandboxContainerError,
            match=r"Docker container command timed out after 30s",
        ):
            _run_docker_command(["docker", "run", "-d"], timeout=30)


def test_container_manager_start_raises_when_docker_run_fails(tmp_path: Path) -> None:
    """Container start should fail cleanly when `docker run` exits non-zero."""
    manager = DockerSandboxContainerManager(
        command_runner=lambda command, timeout: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="image not found",
        )
    )

    with pytest.raises(DockerSandboxContainerError, match="Failed to start persistent sandbox"):
        manager.start(DockerSandboxContainerRequest(workspace=_workspace_handle(tmp_path)))


def test_container_manager_stop_ignores_missing_container(tmp_path: Path) -> None:
    """Stopping a container that is already gone should be treated as success."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name=build_container_name(workspace),
        image="python:3.12-slim",
        working_dir=str(workspace.workspace_path.resolve()),
    )
    manager = DockerSandboxContainerManager(
        command_runner=lambda command, timeout: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="No such container",
        )
    )

    manager.stop(container)


def test_container_manager_stop_raises_for_other_remove_errors(tmp_path: Path) -> None:
    """Unexpected `docker rm` failures should surface to callers."""
    workspace = _workspace_handle(tmp_path)
    container = DockerSandboxContainer(
        workspace=workspace,
        container_name=build_container_name(workspace),
        image="python:3.12-slim",
        working_dir=str(workspace.workspace_path.resolve()),
    )
    manager = DockerSandboxContainerManager(
        command_runner=lambda command, timeout: subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="permission denied",
        )
    )

    with pytest.raises(DockerSandboxContainerError, match="Failed to stop sandbox container"):
        manager.stop(container)


def test_build_docker_container_run_command_blocks_workspace_root(tmp_path: Path) -> None:
    from sandbox.workspace import default_workspace_root

    workspace = _workspace_handle(tmp_path)
    workspace.repo_url = f"file://{default_workspace_root().resolve()}"
    request = DockerSandboxContainerRequest(workspace=workspace)
    with pytest.raises(DockerSandboxContainerError, match="Mounting the workspace root"):
        _build_docker_container_run_command(request, image="python:3.12-slim")


def test_build_docker_container_run_command_blocks_sibling_workspace(tmp_path: Path) -> None:
    from sandbox.workspace import default_workspace_root

    workspace = _workspace_handle(tmp_path)
    sibling_path = default_workspace_root().resolve() / "workspace-other"
    workspace.repo_url = f"file://{sibling_path}"
    request = DockerSandboxContainerRequest(workspace=workspace)
    with pytest.raises(
        DockerSandboxContainerError,
        match="Mounting sibling workspaces is forbidden",
    ):
        _build_docker_container_run_command(request, image="python:3.12-slim")


def test_build_docker_container_run_command_blocks_outside_root(tmp_path: Path) -> None:
    workspace = _workspace_handle(tmp_path)
    outside_path = tmp_path / "outside-dir"
    workspace.repo_url = f"file://{outside_path.resolve()}"
    request = DockerSandboxContainerRequest(workspace=workspace)
    with pytest.raises(DockerSandboxContainerError, match="outside the allowed workspace root"):
        _build_docker_container_run_command(request, image="python:3.12-slim")


def test_build_docker_container_run_command_allows_outside_root_with_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace_handle(tmp_path)
    outside_path = tmp_path / "allowed-remote-dir"
    outside_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODE_AGENT_ALLOWED_LOCAL_REMOTES", str(tmp_path.resolve()))
    workspace.repo_url = f"file://{outside_path.resolve()}"
    request = DockerSandboxContainerRequest(workspace=workspace)
    # Should not raise
    command = _build_docker_container_run_command(request, image="python:3.12-slim")
    assert "--mount" in command


def test_build_docker_container_run_command_uses_patched_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Container validation should honor the runtime workspace root lookup."""
    import sandbox.workspace as workspace_module

    workspace = _workspace_handle(tmp_path / "actual-workspace")
    local_remote = tmp_path / "dummy_repo"
    local_remote.mkdir()
    monkeypatch.setattr(workspace_module, "default_workspace_root", lambda: tmp_path)
    workspace.repo_url = f"file://{local_remote.resolve()}"
    request = DockerSandboxContainerRequest(workspace=workspace)

    command = _build_docker_container_run_command(request, image="python:3.12-slim")

    local_remote_mount = (
        f"type=bind,source={local_remote.resolve()},target={local_remote.resolve()}"
    )
    assert local_remote_mount in command
