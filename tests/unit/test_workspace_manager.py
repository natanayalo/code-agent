"""Unit tests for sandbox workspace helpers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

import sandbox.workspace as workspace_module
from sandbox.workspace import (
    DEFAULT_WORKSPACE_ROOT_ENV_VAR,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceRequest,
    _build_clone_command,
    _is_github_repo_url,
    _redact_git_error_message,
    _run_command,
    _should_delete_workspace,
    _slugify_task_id,
    build_authenticated_github_git_env,
    default_workspace_root,
)


def test_slugify_task_id_normalizes_symbols() -> None:
    """Workspace ids should be filesystem-safe and predictable."""
    assert _slugify_task_id("Task 30 / Sandbox!") == "task-30-sandbox"


def test_default_workspace_root_slugifies_username_when_getuid_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared default workspace root should slugify non-POSIX usernames."""
    monkeypatch.delattr(workspace_module.os, "getuid", raising=False)
    monkeypatch.setenv("USER", "Team Lead!")
    monkeypatch.setenv("USERNAME", "")

    root = default_workspace_root()

    assert root.name == "code-agent-workspaces-user-team-lead"


def test_default_workspace_root_ignores_whitespace_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace-only env overrides should be treated as unset."""
    monkeypatch.setenv(DEFAULT_WORKSPACE_ROOT_ENV_VAR, "   ")

    root = default_workspace_root()

    assert root.name.startswith("code-agent-workspaces-")
    assert root != Path("   ").expanduser()


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

    def fake_runner(command: list[str], *, cwd: Path | None = None, timeout: int = 300) -> None:
        del cwd
        del timeout
        captured_commands.append(command)
        # T-180: The workspace_path is now pre-created by the manager,
        # so we allow it to exist in the fake runner.
        Path(command[-1]).mkdir(parents=True, exist_ok=True)

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


def test_workspace_manager_uses_init_mode(tmp_path: Path) -> None:
    captured_commands: list[list[str]] = []

    def fake_runner(command: list[str], *, cwd: Path | None = None, timeout: int = 300) -> None:
        captured_commands.append(command)

    manager = WorkspaceManager(tmp_path, command_runner=fake_runner)
    workspace = manager.create_workspace(
        WorkspaceRequest(task_id="task-init", workspace_mode=workspace_module.WorkspaceMode.INIT)
    )

    assert captured_commands == [["git", "init"]]
    assert workspace.workspace_mode == workspace_module.WorkspaceMode.INIT


def test_workspace_manager_uses_none_mode(tmp_path: Path) -> None:
    captured_commands: list[list[str]] = []

    def fake_runner(command: list[str], *, cwd: Path | None = None, timeout: int = 300) -> None:
        captured_commands.append(command)

    manager = WorkspaceManager(tmp_path, command_runner=fake_runner)
    workspace = manager.create_workspace(
        WorkspaceRequest(task_id="task-none", workspace_mode=workspace_module.WorkspaceMode.NONE)
    )

    assert captured_commands == []
    assert workspace.workspace_mode == workspace_module.WorkspaceMode.NONE


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


def test_run_command_truncates_long_output(monkeypatch: pytest.MonkeyPatch) -> None:
    long_output = "x" * 2000

    def mock_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["fail"], returncode=1, stdout=long_output, stderr=""
        )

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(WorkspaceManagerError) as exc_info:
        _run_command(["fail"])

    # Output should be truncated to 1024 chars + "... (truncated)"
    assert len(exc_info.value.args[0]) < 1100
    assert "... (truncated)" in exc_info.value.args[0]


def test_create_workspace_cleans_up_on_failure(tmp_path: Path) -> None:
    def failing_runner(command: list[str], *, cwd: Path | None = None, timeout: int = 300) -> None:
        raise RuntimeError("simulated clone failure")

    manager = WorkspaceManager(tmp_path, command_runner=failing_runner)
    request = WorkspaceRequest(task_id="test", repo_url="http://fake")

    with pytest.raises(RuntimeError, match="simulated clone failure"):
        manager.create_workspace(request)

    assert not list(tmp_path.iterdir())


def test_create_workspace_raises_on_existing_non_empty_directory_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkspaceManager(tmp_path)

    def mock_build_workspace_id(task_id: str, attempt: int) -> str:
        return "workspace-existing"

    manager._command_runner = lambda cmd, **kwargs: None
    monkeypatch.setattr(workspace_module, "_build_workspace_id", mock_build_workspace_id)

    workspace_dir = tmp_path / "workspace-existing"
    workspace_dir.mkdir()
    # Add a dummy file to make it non-empty
    (workspace_dir / "dummy").touch()

    request = WorkspaceRequest(task_id="test", repo_url="http://fake")
    # Should raise error since directory is not empty and has no .git
    with pytest.raises(
        WorkspaceManagerError,
        match="Workspace directory exists and is not empty: workspace-existing",
    ):
        manager.create_workspace(request)


def test_create_workspace_uses_request_cleanup_policy(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    manager._command_runner = lambda cmd, **kwargs: None
    custom_policy = WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=False)
    request = WorkspaceRequest(task_id="test", repo_url="foo", cleanup_policy=custom_policy)
    handle = manager.create_workspace(request)
    assert handle.cleanup_policy is custom_policy

    request2 = WorkspaceRequest(task_id="test2", repo_url="foo")
    handle2 = manager.create_workspace(request2)
    assert handle2.cleanup_policy is manager.cleanup_policy


def test_create_workspace_uses_configurable_timeout(tmp_path: Path) -> None:
    captured_kwargs = {}

    def tracking_runner(command: list[str], **kwargs) -> None:
        captured_kwargs.update(kwargs)

    manager = WorkspaceManager(tmp_path, command_timeout=450, command_runner=tracking_runner)
    request = WorkspaceRequest(task_id="test", repo_url="foo")
    manager.create_workspace(request)

    assert captured_kwargs.get("timeout") == 450


def test_cleanup_workspace_refuses_outside_root(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    handle = WorkspaceHandle(
        workspace_id="test-1",
        task_id="test",
        workspace_path=tmp_path.parent,
        repo_path=tmp_path.parent,
        repo_url="http://fake",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )

    with pytest.raises(WorkspaceManagerError, match="Refusing to delete path outside root"):
        manager.cleanup_workspace(handle, succeeded=True)


def test_cleanup_workspace_handles_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = WorkspaceManager(tmp_path)

    def fake_runner(command: list[str], *, cwd: Path | None = None, timeout: int = 300) -> None:
        pass

    manager._command_runner = fake_runner

    workspace = manager.create_workspace(WorkspaceRequest(task_id="test", repo_url="http://fake"))

    def mock_rmtree(path, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(shutil, "rmtree", mock_rmtree)

    with pytest.raises(WorkspaceManagerError, match="Failed to remove workspace"):
        manager.cleanup_workspace(workspace, succeeded=True)


def test_cleanup_workspace_succeeds(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    manager._command_runner = lambda cmd, **kwargs: None

    workspace = manager.create_workspace(WorkspaceRequest(task_id="test", repo_url="http://fake"))
    assert workspace.workspace_path.exists()

    result = manager.cleanup_workspace(workspace, succeeded=True)
    assert result is True
    assert not workspace.workspace_path.exists()


def test_cleanup_workspace_succeeds_when_already_deleted(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    manager._command_runner = lambda cmd, **kwargs: None

    workspace = manager.create_workspace(WorkspaceRequest(task_id="test", repo_url="http://fake"))

    shutil.rmtree(workspace.workspace_path)

    result = manager.cleanup_workspace(workspace, succeeded=True)
    assert result is True


def test_is_github_repo_url() -> None:
    assert _is_github_repo_url("https://github.com/org/repo.git") is True
    assert _is_github_repo_url("http://github.com/org/repo") is True
    assert _is_github_repo_url("git@github.com:org/repo.git") is True
    assert _is_github_repo_url("github.com/org/repo") is True
    assert _is_github_repo_url("https://gitlab.com/org/repo.git") is False
    assert _is_github_repo_url("https://example.com/repo.git") is False
    assert _is_github_repo_url("") is False
    assert _is_github_repo_url("   ") is False
    assert _is_github_repo_url(None) is False


def test_build_authenticated_github_git_env() -> None:
    # None or empty token leaves config untouched
    empty_env = build_authenticated_github_git_env(None, base_env={"EXISTING": "1"})
    assert empty_env["EXISTING"] == "1"
    assert empty_env["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_COUNT" not in empty_env

    # Valid token adds Authorization header via git config
    auth_env = build_authenticated_github_git_env("secret_pat_value", base_env={})
    assert auth_env["GIT_TERMINAL_PROMPT"] == "0"
    assert auth_env["GIT_CONFIG_COUNT"] == "1"
    assert auth_env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "Authorization: Basic " in auth_env["GIT_CONFIG_VALUE_0"]
    import base64

    b64_val = auth_env["GIT_CONFIG_VALUE_0"].split("Authorization: Basic ")[1]
    assert base64.b64decode(b64_val).decode() == "x-access-token:secret_pat_value"

    # Preserves and appends to existing git configs
    extended_env = build_authenticated_github_git_env(
        "second_pat",
        base_env={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        },
    )
    assert extended_env["GIT_CONFIG_COUNT"] == "2"
    assert extended_env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert extended_env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraheader"

    # Updates in-place if already present
    updated_env = build_authenticated_github_git_env(
        "updated_pat",
        base_env={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "old_value",
        },
    )
    assert updated_env["GIT_CONFIG_COUNT"] == "1"
    assert updated_env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "old_value" not in updated_env["GIT_CONFIG_VALUE_0"]


def test_redact_git_error_message() -> None:
    token = "test_dummy_token_123"
    auth_env = build_authenticated_github_git_env(token, base_env={})
    raw_error = (
        "fatal: unable to access 'https://github.com/org/repo.git/': "
        "Authorization: Basic eC1hY2Nlc3MtdG9rZW46dGVzdF9kdW1teV90b2tlbl8xMjM="
    )
    redacted = _redact_git_error_message(raw_error, auth_env)
    assert token not in redacted
    assert "eC1hY2Nlc3MtdG9rZW46dGVzdF9kdW1teV90b2tlbl8xMjM=" not in redacted
    assert "[REDACTED]" in redacted


def test_workspace_request_masks_git_token_in_repr() -> None:
    request = WorkspaceRequest(
        task_id="test",
        repo_url="https://github.com/org/repo.git",
        git_token="super_secret_github_token",
    )
    repr_str = repr(request)
    assert "super_secret_github_token" not in repr_str
    assert "git_token" not in repr_str


def test_create_workspace_passes_auth_env_for_github_with_token(tmp_path: Path) -> None:
    captured_kwargs: dict[str, Any] = {}

    def runner(cmd: list[str], **kwargs: Any) -> None:
        captured_kwargs.update(kwargs)

    manager = WorkspaceManager(tmp_path, command_runner=runner)
    request = WorkspaceRequest(
        task_id="test-github-auth",
        repo_url="https://github.com/org/private-repo.git",
        git_token="test_dummy_token_123",
    )
    manager.create_workspace(request)

    assert "env" in captured_kwargs
    passed_env = captured_kwargs["env"]
    assert passed_env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "Authorization: Basic " in passed_env["GIT_CONFIG_VALUE_0"]


def test_create_workspace_skips_auth_env_for_non_github_or_empty_token(tmp_path: Path) -> None:
    captured_kwargs: dict[str, Any] = {}

    def runner(cmd: list[str], **kwargs: Any) -> None:
        captured_kwargs.clear()
        captured_kwargs.update(kwargs)

    manager = WorkspaceManager(tmp_path, command_runner=runner)

    # Non-GitHub URL with token
    manager.create_workspace(
        WorkspaceRequest(
            task_id="test-gitlab",
            repo_url="https://gitlab.com/org/repo.git",
            git_token="some_token",
        )
    )
    assert "env" not in captured_kwargs

    # GitHub URL with no token
    manager.create_workspace(
        WorkspaceRequest(
            task_id="test-no-token",
            repo_url="https://github.com/org/public-repo.git",
            git_token=None,
        )
    )
    assert "env" not in captured_kwargs
