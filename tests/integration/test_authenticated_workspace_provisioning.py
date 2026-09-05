"""Integration tests for authenticated workspace provisioning."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from sandbox import (
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceRequest,
)
from sandbox.container import (
    DockerSandboxContainerRequest,
    _build_docker_container_run_command,
)


def _run_git(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _create_source_repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "source-repo"
    repo_path.mkdir()
    _run_git(["git", "init", "--initial-branch", "main"], cwd=repo_path)
    (repo_path / "README.md").write_text("hello", encoding="utf-8")
    _run_git(["git", "add", "README.md"], cwd=repo_path)
    _run_git(
        [
            "git",
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=tester@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo_path,
    )
    return repo_path


def test_workspace_clone_does_not_persist_git_token_in_git_config(tmp_path: Path) -> None:
    """Cloning a repository with git_token must not persist any token in .git/config."""
    source_repo = _create_source_repo(tmp_path)
    workspaces_dir = tmp_path / "workspaces"
    manager = WorkspaceManager(workspaces_dir)

    secret_token = "test_dummy_broker_token_98765"
    handle = manager.create_workspace(
        WorkspaceRequest(
            task_id="auth-task",
            repo_url=str(source_repo),
            branch="main",
            git_token=secret_token,
            cleanup_policy=WorkspaceCleanupPolicy(retain_on_failure=True),
        )
    )

    assert handle.repo_path.exists()
    git_config_path = handle.repo_path / ".git" / "config"
    assert git_config_path.exists()
    config_content = git_config_path.read_text(encoding="utf-8")

    assert secret_token not in config_content
    assert "extraheader" not in config_content
    assert "Authorization" not in config_content

    # Also ensure no token is written anywhere inside .git/
    for git_file in (handle.repo_path / ".git").rglob("*"):
        if git_file.is_file():
            try:
                content = git_file.read_text(encoding="utf-8", errors="ignore")
                assert secret_token not in content
            except Exception:
                pass


def test_child_container_does_not_inherit_broker_git_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker container command generation must not leak host/broker git tokens."""
    monkeypatch.setenv("GH_TOKEN", "broker-host-gh-token-secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.https://github.com/.extraheader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Basic secret-auth-header")

    handle = WorkspaceHandle(
        workspace_id="test-workspace-id",
        task_id="container-test",
        workspace_path=tmp_path,
        repo_path=tmp_path,
        repo_url="https://github.com/org/repo.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )
    req = DockerSandboxContainerRequest(
        workspace=handle,
        environment={"USER_ENV": "allowed_value"},
    )
    docker_cmd = _build_docker_container_run_command(req, image="python:3.12-slim")
    cmd_str = " ".join(docker_cmd)

    assert "broker-host-gh-token-secret" not in cmd_str
    assert "secret-auth-header" not in cmd_str
    assert "GIT_CONFIG" not in cmd_str
    assert "--env USER_ENV=allowed_value" in cmd_str


def test_authenticated_clone_redacts_credentials_on_failure(tmp_path: Path) -> None:
    """Failed clones must redact any token or Authorization header in the error message."""
    manager = WorkspaceManager(tmp_path / "workspaces")
    secret_token = "test_dummy_failing_token_999"

    # Use a non-existent github repo to trigger a git clone failure
    request = WorkspaceRequest(
        task_id="failed-auth-task",
        repo_url="https://github.com/natanayalo/non-existent-repo-for-testing-12345.git",
        branch="main",
        git_token=secret_token,
    )

    with pytest.raises(WorkspaceManagerError) as exc_info:
        manager.create_workspace(request)

    error_msg = str(exc_info.value)
    assert secret_token not in error_msg
    encoded = base64.b64encode(f"x-access-token:{secret_token}".encode()).decode()
    assert encoded not in error_msg
