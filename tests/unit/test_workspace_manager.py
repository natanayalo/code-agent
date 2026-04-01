"""Unit tests for sandbox workspace helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sandbox.workspace import (
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceRequest,
    _build_clone_command,
    _run_command,
    _should_delete_workspace,
    _slugify_task_id,
)


def test_slugify_task_id_normalizes_symbols() -> None:
    """Workspace ids should be filesystem-safe and predictable."""
    assert _slugify_task_id("Task 30 / Sandbox!") == "task-30-sandbox"


def test_build_clone_command_adds_branch_when_requested() -> None:
    """Branch-aware clones should use single-branch checkout."""
    command = _build_clone_command("https://example.com/repo.git", Path("/tmp/repo"), "main")

    assert command == [
        "git",
        "clone",
        "--branch",
        "main",
        "--single-branch",
        "--",
        "https://example.com/repo.git",
        "/tmp/repo",
    ]


def test_cleanup_policy_deletes_successful_workspace() -> None:
    """Successful runs should be removable under the default policy."""
    assert _should_delete_workspace(WorkspaceCleanupPolicy(), succeeded=True) is True


def test_cleanup_policy_retains_failed_workspace_by_default() -> None:
    """Failed workspaces are kept by default for debugging."""
    assert _should_delete_workspace(WorkspaceCleanupPolicy(), succeeded=False) is False


def test_workspace_manager_uses_injected_command_runner(tmp_path: Path) -> None:
    """Workspace creation should delegate clone execution through the runner boundary."""
    captured_commands: list[list[str]] = []

    def fake_runner(command: list[str], *, cwd: Path | None = None) -> None:
        del cwd
        captured_commands.append(command)
        Path(command[-1]).mkdir(parents=True, exist_ok=False)

    manager = WorkspaceManager(tmp_path, command_runner=fake_runner)
    workspace = manager.create_workspace(
        WorkspaceRequest(task_id="task-30", repo_url="/tmp/source-repo", branch="main")
    )

    assert workspace.workspace_path.exists()
    assert workspace.repo_path.exists()
    assert captured_commands == [
        [
            "git",
            "clone",
            "--branch",
            "main",
            "--single-branch",
            "--",
            "/tmp/source-repo",
            str(workspace.repo_path),
        ]
    ]


def test_run_command_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sleep", timeout=300)

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(WorkspaceManagerError, match=r"Command timed out after 300s"):
        _run_command(["sleep", "400"])


def test_run_command_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["fail"], returncode=1, stdout="", stderr="mock error"
        )

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(WorkspaceManagerError, match=r"Command failed \(fail\): mock error"):
        _run_command(["fail"])


def test_create_workspace_cleans_up_on_failure(tmp_path: Path) -> None:
    def failing_runner(command: list[str], *, cwd: Path | None = None) -> None:
        raise RuntimeError("simulated clone failure")

    manager = WorkspaceManager(tmp_path, command_runner=failing_runner)
    request = WorkspaceRequest(task_id="test", repo_url="http://fake")

    with pytest.raises(RuntimeError, match="simulated clone failure"):
        manager.create_workspace(request)

    assert not list(tmp_path.iterdir())


def test_create_workspace_raises_on_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkspaceManager(tmp_path)

    import sandbox.workspace

    def mock_build_workspace_id(task_id: str) -> str:
        return "workspace-collision"

    manager._command_runner = lambda cmd, **kwargs: None
    monkeypatch.setattr(sandbox.workspace, "_build_workspace_id", mock_build_workspace_id)

    (tmp_path / "workspace-collision").mkdir()

    request = WorkspaceRequest(task_id="test", repo_url="http://fake")
    with pytest.raises(WorkspaceManagerError, match="Workspace directory already exists"):
        manager.create_workspace(request)


def test_cleanup_workspace_refuses_outside_root(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    handle = WorkspaceHandle(
        workspace_id="test-1",
        task_id="test",
        workspace_path=tmp_path.parent,
        repo_path=tmp_path.parent / "repo",
        repo_url="http://fake",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )

    with pytest.raises(WorkspaceManagerError, match="Refusing to delete path outside root"):
        manager.cleanup_workspace(handle, succeeded=True)


def test_cleanup_workspace_handles_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkspaceManager(tmp_path)

    def fake_runner(command: list[str], *, cwd: Path | None = None) -> None:
        pass

    manager._command_runner = fake_runner

    workspace = manager.create_workspace(WorkspaceRequest(task_id="test", repo_url="http://fake"))

    import shutil

    def mock_rmtree(path, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(shutil, "rmtree", mock_rmtree)

    with pytest.raises(WorkspaceManagerError, match="Failed to remove workspace"):
        manager.cleanup_workspace(workspace, succeeded=True)
