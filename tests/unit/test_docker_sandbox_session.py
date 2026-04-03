"""Unit tests for persistent sandbox shell sessions."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from sandbox.container import DockerSandboxContainer
from sandbox.session import DockerShellSession, DockerShellSessionError
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle


def _workspace_handle(tmp_path: Path) -> WorkspaceHandle:
    workspace_path = tmp_path / "workspace-task-45"
    repo_path = workspace_path / "repo"
    repo_path.mkdir(parents=True)
    return WorkspaceHandle(
        workspace_id="workspace-task-45",
        task_id="task-45",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


def _container_handle(tmp_path: Path) -> DockerSandboxContainer:
    return DockerSandboxContainer(
        workspace=_workspace_handle(tmp_path),
        container_name="sandbox-workspace-task-45",
        image="python:3.12-slim",
    )


def _local_shell_process_factory(command: list[str]) -> subprocess.Popen[bytes]:
    del command
    return subprocess.Popen(
        ["/bin/sh"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def test_shell_session_preserves_environment_and_working_directory(tmp_path: Path) -> None:
    """Environment exports and directory changes should persist across commands."""
    container = _container_handle(tmp_path)

    with DockerShellSession(container, process_factory=_local_shell_process_factory) as session:
        first = session.execute("export GREETING=hello")
        second = session.execute('printf "%s\\n" "$GREETING"')
        session.execute(f"cd {shlex.quote(str(tmp_path))}")
        third = session.execute("pwd")
        fourth = session.execute("printf 'done\\n'")

    assert first.exit_code == 0
    assert first.output == ""
    assert second.output == "hello\n"
    assert third.output.strip() == str(tmp_path)
    assert fourth.output == "done\n"


def test_shell_session_times_out_and_closes(tmp_path: Path) -> None:
    """A timed-out command should kill the session instead of hanging indefinitely."""
    container = _container_handle(tmp_path)

    session = DockerShellSession(container, process_factory=_local_shell_process_factory)
    with pytest.raises(DockerShellSessionError, match="timed out after 1s"):
        session.execute("sleep 2", timeout_seconds=1)

    with pytest.raises(DockerShellSessionError, match="closed"):
        session.execute("printf 'still here\\n'")


def test_shell_session_enforces_output_limit(tmp_path: Path) -> None:
    """Command output should be bounded to avoid runaway shell sessions."""
    container = _container_handle(tmp_path)

    session = DockerShellSession(
        container,
        process_factory=_local_shell_process_factory,
        output_limit_bytes=32,
    )
    with pytest.raises(DockerShellSessionError, match="output limit exceeded"):
        session.execute("printf 'abcdefghijklmnopqrstuvwxyz0123456789'")
