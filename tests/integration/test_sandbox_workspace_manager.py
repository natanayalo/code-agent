"""Integration tests for sandbox workspace management."""

from __future__ import annotations

import subprocess
from pathlib import Path

from sandbox import WorkspaceManager, WorkspaceRequest


def _run_git(command: list[str], *, cwd: Path) -> str:
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
    _run_git(["git", "checkout", "-b", "feature/workspace"], cwd=repo_path)
    (repo_path / "feature.txt").write_text("feature branch\n", encoding="utf-8")
    _run_git(["git", "add", "feature.txt"], cwd=repo_path)
    _run_git(
        [
            "git",
            "-c",
            "user.name=Codex",
            "-c",
            "user.email=codex@example.com",
            "commit",
            "-m",
            "feature",
        ],
        cwd=repo_path,
    )
    _run_git(["git", "checkout", "main"], cwd=repo_path)
    return repo_path


def test_workspace_manager_clones_repo_into_unique_workspace(tmp_path: Path) -> None:
    """A task workspace contains a clone of the requested repo and branch."""
    source_repo = _create_local_repo(tmp_path)
    manager = WorkspaceManager(tmp_path / "workspaces")

    workspace = manager.create_workspace(
        WorkspaceRequest(
            task_id="task-30",
            repo_url=str(source_repo),
            branch="feature/workspace",
        )
    )

    head_branch = _run_git(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=workspace.repo_path,
    )

    assert workspace.workspace_path.parent == (tmp_path / "workspaces").resolve()
    assert workspace.repo_path.exists()
    assert (workspace.repo_path / "README.md").exists()
    assert (workspace.repo_path / "feature.txt").exists()
    assert head_branch == "feature/workspace"


def test_workspace_manager_cleanup_policy_removes_successful_workspaces(tmp_path: Path) -> None:
    """Successful workspaces are deleted while failed ones are retained by default."""
    source_repo = _create_local_repo(tmp_path)
    manager = WorkspaceManager(tmp_path / "workspaces")

    successful_workspace = manager.create_workspace(
        WorkspaceRequest(task_id="task-success", repo_url=str(source_repo))
    )
    failed_workspace = manager.create_workspace(
        WorkspaceRequest(task_id="task-failure", repo_url=str(source_repo))
    )

    deleted_successful = manager.cleanup_workspace(successful_workspace, succeeded=True)
    deleted_failed = manager.cleanup_workspace(failed_workspace, succeeded=False)

    assert deleted_successful is True
    assert not successful_workspace.workspace_path.exists()
    assert deleted_failed is False
    assert failed_workspace.workspace_path.exists()
