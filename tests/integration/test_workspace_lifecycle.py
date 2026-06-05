"""Integration tests for persistent workspace lifecycle and read-only enforcement."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sandbox import WorkspaceManager, WorkspaceManagerError, WorkspaceRequest
from workers.cli_runtime import _looks_read_only_command


def _run_git(command: list[str], *, cwd: Path) -> str:
    import subprocess

    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _create_local_repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "source-repo"
    repo_path.mkdir()
    _run_git(["git", "init", "--initial-branch", "main"], cwd=repo_path)
    (repo_path / "README.md").write_text("main branch\n", encoding="utf-8")
    _run_git(["git", "add", "README.md"], cwd=repo_path)
    _run_git(
        [
            "git",
            "-c",
            "user.name=Codex",
            "-c",
            "user.email=codex@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo_path,
    )
    return repo_path


def test_workspace_manager_get_workspace_reuse(tmp_path: Path) -> None:
    """Verify that get_workspace retrieves the same workspace and fails if missing."""
    source_repo = _create_local_repo(tmp_path)
    workspaces_root = tmp_path / "workspaces"
    manager = WorkspaceManager(workspaces_root)

    # 1. Create a workspace
    request = WorkspaceRequest(task_id="test-task", repo_url=str(source_repo))
    handle = manager.create_workspace(request)

    workspace_id = handle.workspace_id

    # 2. Retrieve it
    retrieved = manager.get_workspace(workspace_id)
    assert retrieved.workspace_id == workspace_id
    assert retrieved.workspace_path == handle.workspace_path
    assert retrieved.repo_path == handle.repo_path

    # 3. Fail if missing (Hard-fail policy T-178)
    shutil.rmtree(handle.workspace_path)
    with pytest.raises(WorkspaceManagerError, match="Workspace directory missing"):
        manager.get_workspace(workspace_id)


def test_workspace_manager_get_workspace_prevents_traversal(tmp_path: Path) -> None:
    """Verify that get_workspace prevents directory traversal."""
    workspaces_root = tmp_path / "workspaces"
    workspaces_root.mkdir()
    manager = WorkspaceManager(workspaces_root)

    with pytest.raises(WorkspaceManagerError, match="Refusing to access path outside root"):
        manager.get_workspace("../secret")


def test_read_only_command_classification() -> None:
    """Verify that _looks_read_only_command correctly identifies write operations."""
    # Positive cases (Read-only)
    assert _looks_read_only_command("cat README.md") is True
    assert _looks_read_only_command("ls -la") is True
    assert _looks_read_only_command("grep 'hello' main.py") is True
    assert _looks_read_only_command("git status") is True
    assert _looks_read_only_command("pwd") is True

    # Negative cases (Write/Destructive)
    assert _looks_read_only_command("rm -rf /") is False
    assert _looks_read_only_command("echo 'hi' > file.txt") is False
    assert _looks_read_only_command("chmod +x script.sh") is False
    assert _looks_read_only_command("mv old.txt new.txt") is False
    assert _looks_read_only_command("cp file.txt backup.txt") is False
    assert _looks_read_only_command("git add .") is False
    assert _looks_read_only_command("git commit -m 'feat'") is False
    assert _looks_read_only_command("sed -i 's/a/b/' file.txt") is False
