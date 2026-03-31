"""Unit tests for sandbox workspace helpers."""

from __future__ import annotations

from pathlib import Path

from sandbox.workspace import (
    WorkspaceCleanupPolicy,
    WorkspaceManager,
    WorkspaceRequest,
    _build_clone_command,
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
