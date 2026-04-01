"""Unit tests for the Docker sandbox runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sandbox.runner import (
    DockerSandboxCommand,
    DockerSandboxRunner,
    DockerSandboxRunnerError,
    _build_docker_run_command,
    _run_docker_command,
)
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle


def _workspace_handle(tmp_path: Path) -> WorkspaceHandle:
    workspace_path = tmp_path / "workspace-task-31"
    repo_path = workspace_path / "repo"
    repo_path.mkdir(parents=True)
    return WorkspaceHandle(
        workspace_id="workspace-task-31",
        task_id="task-31",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


def test_build_docker_run_command_mounts_workspace_and_disables_network(tmp_path: Path) -> None:
    """Docker commands should mount the workspace and isolate network by default."""
    request = DockerSandboxCommand(
        workspace=_workspace_handle(tmp_path),
        command=["python3", "-c", "print('sandbox')"],
        environment={"PYTHONUNBUFFERED": "1"},
    )

    command = _build_docker_run_command(request, image="python:3.12-slim")

    expected_command = [
        "docker",
        "run",
        "--rm",
        "--memory",
        "1g",
        "--cpus",
        "1.0",
        "--workdir",
        "/workspace/repo",
        "--mount",
        f"type=bind,source='{request.workspace.workspace_path.resolve()}',target=/workspace",
    ]
    try:
        import os

        uid = os.getuid()
        gid = os.getgid()
        expected_command.extend(["--user", f"{uid}:{gid}"])
    except AttributeError:
        pass

    expected_command.extend(
        [
            "--network",
            "none",
            "--env",
            "PYTHONUNBUFFERED=1",
            "python:3.12-slim",
            "python3",
            "-c",
            "print('sandbox')",
        ]
    )

    assert command == expected_command


def test_build_docker_run_command_skips_user_mapping_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.getuid is missing (like on Windows), we should skip user mapping gracefully."""
    import os

    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.delattr(os, "getgid", raising=False)

    request = DockerSandboxCommand(
        workspace=_workspace_handle(tmp_path),
        command=["echo", "test"],
    )

    command = _build_docker_run_command(request, image="alpine")

    assert "--user" not in command
    assert command == [
        "docker",
        "run",
        "--rm",
        "--memory",
        "1g",
        "--cpus",
        "1.0",
        "--workdir",
        "/workspace/repo",
        "--mount",
        f"type=bind,source='{request.workspace.workspace_path.resolve()}',target=/workspace",
        "--network",
        "none",
        "alpine",
        "echo",
        "test",
    ]


def test_runner_returns_structured_result(tmp_path: Path) -> None:
    """A successful docker invocation should return captured stdout/stderr."""
    request = DockerSandboxCommand(
        workspace=_workspace_handle(tmp_path),
        command=["python3", "-c", "print('sandbox')"],
    )

    def fake_runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        assert timeout == 300
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="sandbox\n",
            stderr="",
        )

    runner = DockerSandboxRunner(command_runner=fake_runner)
    result = runner.run(request)

    assert result.image == "python:3.12-slim"
    assert result.command == ["python3", "-c", "print('sandbox')"]
    assert result.exit_code == 0
    assert result.stdout == "sandbox\n"
    assert result.stderr == ""
    assert result.duration_seconds >= 0


def test_runner_uses_request_image_override(tmp_path: Path) -> None:
    """Per-command image overrides should win over the runner default."""
    request = DockerSandboxCommand(
        workspace=_workspace_handle(tmp_path),
        command=["sh", "-c", "echo ok"],
        image="busybox:1.36",
    )

    captured_command: list[str] = []

    def fake_runner(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        captured_command.extend(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    runner = DockerSandboxRunner(default_image="python:3.12-slim", command_runner=fake_runner)
    result = runner.run(request)

    assert result.image == "busybox:1.36"
    assert "busybox:1.36" in captured_command


def test_timeout_raises_with_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeouts should surface as DockerSandboxRunnerError."""

    def mock_run(*args, **kwargs):
        out_file = kwargs.get("stdout")
        if out_file:
            out_file.write(b"timeout out")
        raise subprocess.TimeoutExpired(cmd="docker run", timeout=30)

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(
        DockerSandboxRunnerError,
        match=r"Docker sandbox command timed out after 30s \(docker run image\): timeout out",
    ):
        _run_docker_command(["docker", "run", "image"], timeout=30)


def test_timeout_decorates_bytes_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte output from subrpocess shouldn't crash the error formatter on timeout."""

    def mock_run(*args, **kwargs):
        out_file = kwargs.get("stdout")
        if out_file:
            # We must write bytes because the temporary files are opened in binary mode.
            out_file.write(b"byte out\xff")
        raise subprocess.TimeoutExpired(cmd="docker run", timeout=30)

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(DockerSandboxRunnerError, match=r": byte out"):
        _run_docker_command(["docker", "run", "image"], timeout=30)


def test_timeout_truncates_long_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Massive output from docker shouldn't bloat the logger but must be truncated."""

    long_output = "x" * 2000

    def mock_run(*args, **kwargs):
        out_file = kwargs.get("stdout")
        if out_file:
            out_file.write(long_output.encode("utf-8"))
        raise subprocess.TimeoutExpired(cmd="docker run", timeout=30)

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(DockerSandboxRunnerError) as exc_info:
        _run_docker_command(["docker", "run", "image"], timeout=30)

    assert "... (truncated)" in str(exc_info.value)
    assert len(str(exc_info.value)) < 1150


def test_run_docker_command_truncates_stream_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execution output surpassing 2MB limits should safely truncate."""
    limit = 2 * 1024 * 1024

    def mock_run(*args, **kwargs):
        out_file = kwargs.get("stdout")
        err_file = kwargs.get("stderr")
        if out_file and err_file:
            out_file.write(b"x" * (limit + 100))
            err_file.write(b"y" * (limit + 100))
        return subprocess.CompletedProcess(args=args[0], returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)

    result = _run_docker_command(["docker", "run", "image"], timeout=30)

    assert result.returncode == 0
    assert len(result.stdout) == limit + len("\n... (truncated)")
    assert result.stdout.endswith("... (truncated)")
    assert len(result.stderr) == limit + len("\n... (truncated)")
    assert result.stderr.endswith("... (truncated)")


def test_run_docker_command_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standard executions capture regular logs directly and faithfully."""

    def mock_run(*args, **kwargs):
        out_file = kwargs.get("stdout")
        if out_file:
            out_file.write(b"regular log")
        return subprocess.CompletedProcess(args=args[0], returncode=0)

    monkeypatch.setattr(subprocess, "run", mock_run)

    result = _run_docker_command(["docker", "run", "image"], timeout=30)

    assert result.returncode == 0
    assert result.stdout == "regular log"
    assert result.stderr == ""


def test_run_docker_command_raises_on_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker execution daemon initialization errors should surface as DockerSandboxRunnerError."""

    def mock_run(*args, **kwargs):
        raise OSError("Docker daemon missing")

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(
        DockerSandboxRunnerError,
        match=r"Failed to start Docker sandbox command \(docker run image\): Docker daemon missing",
    ):
        _run_docker_command(["docker", "run", "image"], timeout=30)
