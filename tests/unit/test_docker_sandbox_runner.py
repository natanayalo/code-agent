"""Unit tests for the Docker sandbox runner."""

from __future__ import annotations

import io
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sandbox.audit import (
    parse_git_status_entries as _parse_git_status_entries,
)
from sandbox.policy import PathPolicy
from sandbox.redact import SecretRedactor
from sandbox.runner import (
    MAX_OUTPUT_SIZE_BYTES,
    DockerSandboxCommand,
    DockerSandboxOutputLimitError,
    DockerSandboxRunner,
    DockerSandboxRunnerError,
    _build_container_name,
    _build_docker_run_command,
    _run_docker_command,
)
from sandbox.streams import read_stream_bounded as _read_stream_bounded
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle


def _run_git(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _initialize_repo(repo_path: Path) -> None:
    _run_git(["git", "init", "--initial-branch", "main"], cwd=repo_path)
    (repo_path / "README.md").write_text("sandbox runner\n", encoding="utf-8")
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


def _workspace_handle(tmp_path: Path, *, initialize_git: bool = False) -> WorkspaceHandle:
    workspace_path = tmp_path / "workspace-task-31"
    repo_path = workspace_path / "repo"
    repo_path.mkdir(parents=True)
    if initialize_git:
        _initialize_repo(repo_path)
    return WorkspaceHandle(
        workspace_id="workspace-task-31",
        task_id="task-31",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


def _make_popen_mock(
    stdout_data: bytes = b"",
    stderr_data: bytes = b"",
    returncode: int = 0,
    wait_side_effect: Exception | None = None,
) -> MagicMock:
    """Build a mock subprocess.Popen object with streaming stdout/stderr."""
    mock = MagicMock()
    mock.stdout = io.BytesIO(stdout_data)
    mock.stderr = io.BytesIO(stderr_data)
    mock.returncode = returncode

    if wait_side_effect is not None:
        # First call (proc.wait(timeout=N)) should raise; second call (proc.wait() after
        # proc.kill()) must return normally so threads can join without propagating the exc.
        def _wait_side_effect(*args: object, **kwargs: object) -> int:
            if "timeout" in kwargs:
                raise wait_side_effect  # type: ignore[misc]
            return returncode

        mock.wait.side_effect = _wait_side_effect
    else:
        mock.wait.return_value = returncode

    return mock


# ---------------------------------------------------------------------------
# _build_docker_run_command
# ---------------------------------------------------------------------------


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
        "--name",
        _build_container_name(request.workspace),
        "--memory",
        "1g",
        "--cpus",
        "1.0",
        "--workdir",
        "/workspace/repo",
        "--mount",
        f"type=bind,source={request.workspace.workspace_path.resolve()},target=/workspace",
    ]
    try:
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


def test_build_docker_run_command_raises_on_comma_in_path(tmp_path: Path) -> None:
    """Workspace paths containing commas are incompatible with --mount and must fail fast."""
    # Manually construct a handle whose path would contain a comma.
    workspace_path = tmp_path / "work,space"
    repo_path = workspace_path / "repo"
    repo_path.mkdir(parents=True)
    handle = WorkspaceHandle(
        workspace_id="workspace-task-31",
        task_id="task-31",
        workspace_path=workspace_path,
        repo_path=repo_path,
        repo_url="https://example.com/repo.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )
    request = DockerSandboxCommand(workspace=handle, command=["echo", "hi"])

    with pytest.raises(DockerSandboxRunnerError, match="Workspace path contains a comma"):
        _build_docker_run_command(request, image="alpine")


def test_build_docker_run_command_skips_user_mapping_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.getuid is missing (like on Windows), we should skip user mapping gracefully."""
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
        "--name",
        _build_container_name(request.workspace),
        "--memory",
        "1g",
        "--cpus",
        "1.0",
        "--workdir",
        "/workspace/repo",
        "--mount",
        f"type=bind,source={request.workspace.workspace_path.resolve()},target=/workspace",
        "--network",
        "none",
        "alpine",
        "echo",
        "test",
    ]


# ---------------------------------------------------------------------------
# DockerSandboxRunner.run (uses injected command_runner)
# ---------------------------------------------------------------------------


def test_runner_returns_structured_result(tmp_path: Path) -> None:
    """A successful docker invocation should return captured stdout/stderr."""
    request = DockerSandboxCommand(
        workspace=_workspace_handle(tmp_path, initialize_git=True),
        command=["python3", "-c", "print('sandbox')"],
    )

    def fake_runner(
        command: list[str],
        *,
        timeout: int,
        redactor: SecretRedactor | None = None,
    ) -> subprocess.CompletedProcess[str]:
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
    assert result.files_changed == []

    artifact_paths = {
        artifact.name: request.workspace.workspace_path / artifact.uri
        for artifact in result.artifacts
    }
    assert set(artifact_paths) == {"stdout.log", "stderr.log", "changed-files.txt"}
    assert artifact_paths["stdout.log"].read_text(encoding="utf-8") == "sandbox\n"
    assert artifact_paths["stderr.log"].read_text(encoding="utf-8") == ""
    assert artifact_paths["changed-files.txt"].read_text(encoding="utf-8") == (
        "No changed files detected.\n"
    )


def test_runner_uses_request_image_override(tmp_path: Path) -> None:
    """Per-command image overrides should win over the runner default."""
    request = DockerSandboxCommand(
        workspace=_workspace_handle(tmp_path, initialize_git=True),
        command=["sh", "-c", "echo ok"],
        image="busybox:1.36",
    )

    captured_command: list[str] = []

    def fake_runner(
        command: list[str],
        *,
        timeout: int,
        redactor: SecretRedactor | None = None,
    ) -> subprocess.CompletedProcess[str]:
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


def test_runner_captures_changed_files_and_diff_summary(tmp_path: Path) -> None:
    """Artifact capture should snapshot changed files and a diff summary after the run."""
    workspace = _workspace_handle(tmp_path, initialize_git=True)
    request = DockerSandboxCommand(
        workspace=workspace,
        command=["python3", "-c", "print('changed')"],
    )

    def fake_runner(
        command: list[str],
        *,
        timeout: int,
        redactor: SecretRedactor | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del command, timeout
        (workspace.repo_path / "README.md").write_text("sandbox runner updated\n", encoding="utf-8")
        (workspace.repo_path / "generated.txt").write_text("generated\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=request.command,
            returncode=0,
            stdout="changed\n",
            stderr="warning\n",
        )

    result = DockerSandboxRunner(command_runner=fake_runner).run(request)

    assert set(result.files_changed) == {"README.md", "generated.txt"}

    artifact_paths = {
        artifact.name: workspace.workspace_path / artifact.uri for artifact in result.artifacts
    }
    assert set(artifact_paths) == {
        "stdout.log",
        "stderr.log",
        "changed-files.txt",
        "diff-summary.txt",
    }
    assert artifact_paths["stdout.log"].read_text(encoding="utf-8") == "changed\n"
    assert artifact_paths["stderr.log"].read_text(encoding="utf-8") == "warning\n"
    assert artifact_paths["changed-files.txt"].read_text(encoding="utf-8") in (
        "README.md\ngenerated.txt\n",
        "generated.txt\nREADME.md\n",
    )
    diff_summary = artifact_paths["diff-summary.txt"].read_text(encoding="utf-8")
    assert "README.md | 2 +-" in diff_summary
    assert "Untracked files:" in diff_summary
    assert "- generated.txt" in diff_summary


# ---------------------------------------------------------------------------
# git status parsing
# ---------------------------------------------------------------------------


def test_parse_git_status_entries_handles_rename_and_untracked() -> None:
    """Porcelain output should preserve the destination path for renames."""
    output = "R  old-name.py\0new-name.py\0?? notes.txt\0"

    assert _parse_git_status_entries(output) == [
        ("R ", "new-name.py"),
        ("??", "notes.txt"),
    ]


def test_parse_git_status_entries_falls_back_when_rename_target_is_missing() -> None:
    """Malformed rename entries should keep the original path instead of crashing."""
    output = "R  old-name.py\0"

    assert _parse_git_status_entries(output) == [("R ", "old-name.py")]


# ---------------------------------------------------------------------------
# _run_docker_command – Popen-based tests
# ---------------------------------------------------------------------------


def test_timeout_raises_with_output() -> None:
    """Timeouts should surface as DockerSandboxRunnerError with captured tail output."""
    mock_proc = _make_popen_mock(
        stdout_data=b"timeout out",
        wait_side_effect=subprocess.TimeoutExpired(cmd="docker run", timeout=30),
    )

    with patch("subprocess.Popen", return_value=mock_proc):
        with pytest.raises(
            DockerSandboxRunnerError,
            match=r"(?s)timed out after 30s.*stdout: timeout out",
        ):
            _run_docker_command(["docker", "run", "image"], timeout=30)


def test_timeout_decorates_bytes_output() -> None:
    """Byte output with invalid UTF-8 from subprocess shouldn't crash the error formatter."""
    mock_proc = _make_popen_mock(
        stdout_data=b"byte out\xff",
        wait_side_effect=subprocess.TimeoutExpired(cmd="docker run", timeout=30),
    )

    with patch("subprocess.Popen", return_value=mock_proc):
        with pytest.raises(DockerSandboxRunnerError, match=r"stdout: byte out"):
            _run_docker_command(["docker", "run", "image"], timeout=30)


def test_timeout_truncates_long_output() -> None:
    """Massive output on timeout should be tail-truncated to the last 1024 bytes."""
    long_output = b"x" * 2000

    mock_proc = _make_popen_mock(
        stdout_data=long_output,
        wait_side_effect=subprocess.TimeoutExpired(cmd="docker run", timeout=30),
    )

    with patch("subprocess.Popen", return_value=mock_proc):
        with pytest.raises(DockerSandboxRunnerError) as exc_info:
            _run_docker_command(["docker", "run", "image"], timeout=30)

    assert "tail of captured prefix" in str(exc_info.value)
    assert len(str(exc_info.value)) < 1200


def test_run_docker_command_truncates_stream_limits() -> None:
    """Execution output surpassing MAX_OUTPUT_SIZE_BYTES should trigger kill and error."""
    limit = MAX_OUTPUT_SIZE_BYTES
    mock_proc = _make_popen_mock(
        stdout_data=b"x" * (limit + 100),
        stderr_data=b"y" * (limit + 100),
    )

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("subprocess.run") as mock_run,
    ):
        with pytest.raises(DockerSandboxOutputLimitError, match="output limit exceeded"):
            _run_docker_command(
                ["docker", "run", "--name", "sandbox-workspace-task-31", "image"],
                timeout=30,
            )

    assert mock_proc.kill.called
    mock_run.assert_called_once_with(
        ["docker", "kill", "sandbox-workspace-task-31"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_run_docker_command_success() -> None:
    """Standard executions capture regular logs directly and faithfully."""
    mock_proc = _make_popen_mock(stdout_data=b"regular log")

    with patch("subprocess.Popen", return_value=mock_proc):
        result = _run_docker_command(["docker", "run", "image"], timeout=30)

    assert result.returncode == 0
    assert result.stdout == "regular log"
    assert result.stderr == ""


def test_run_docker_command_raises_on_os_error() -> None:
    """Docker execution daemon initialization errors should surface as DockerSandboxRunnerError."""
    with patch("subprocess.Popen", side_effect=OSError("Docker daemon missing")):
        with pytest.raises(
            DockerSandboxRunnerError,
            match=r"Failed to start Docker sandbox command"
            r" \(docker run image\): Docker daemon missing",
        ):
            _run_docker_command(["docker", "run", "image"], timeout=30)


# ---------------------------------------------------------------------------
# _read_stream_bounded
# ---------------------------------------------------------------------------


def test_read_stream_bounded_under_limit() -> None:
    """Streams smaller than the limit are captured fully."""
    stream = io.BytesIO(b"hello world")
    buf = _read_stream_bounded(stream, limit=100)
    assert buf == bytearray(b"hello world")


def test_read_stream_bounded_at_limit() -> None:
    """Streams equal to the limit fit within limit+1 capacity – no truncation marker."""
    data = b"a" * 100
    stream = io.BytesIO(data)
    buf = _read_stream_bounded(stream, limit=100)
    # Exactly 100 bytes → buf holds 100 bytes, len(buf) == limit, not > limit.
    assert buf == bytearray(data)
    assert len(buf) == 100


def test_read_stream_bounded_over_limit() -> None:
    """Streams exceeding the limit store limit+1 bytes so _decode_bounded detects overflow."""
    limit = 50
    # BytesIO returns all data in one read() if chunk size > len(data).
    data = b"x" * (limit + 10)
    stream = io.BytesIO(data)
    buf = _read_stream_bounded(stream, limit=limit)
    assert len(buf) == limit + 1
    assert buf == bytearray(data[: limit + 1])


def test_read_stream_bounded_on_limit_is_triggered() -> None:
    """The on_limit callback is invoked and reading stops early when limit exceeded."""
    limit = 50
    data = b"x" * 100
    stream = io.BytesIO(data)
    on_limit_called = False

    def on_limit():
        nonlocal on_limit_called
        on_limit_called = True

    buf = _read_stream_bounded(stream, limit=limit, on_limit=on_limit)
    assert on_limit_called
    assert len(buf) == limit + 1


def test_read_stream_bounded_pipe_fully_drained() -> None:
    """Even when over limit, the underlying stream must be fully read to avoid pipe blockage."""
    limit = 10
    data = b"z" * 100000

    class CountingStream:
        def __init__(self, data: bytes) -> None:
            self._stream = io.BytesIO(data)
            self.total_read = 0

        def read(self, n: int) -> bytes:
            chunk = self._stream.read(n)
            self.total_read += len(chunk)
            return chunk

    stream = CountingStream(data)
    _read_stream_bounded(stream, limit=limit)  # type: ignore[arg-type]
    assert stream.total_read == len(data)


def test_run_docker_command_kills_named_container_on_timeout() -> None:
    """Timeout cleanup should attempt to stop the named docker container."""
    mock_proc = _make_popen_mock(
        stdout_data=b"timeout out",
        wait_side_effect=subprocess.TimeoutExpired(cmd="docker run", timeout=30),
    )

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("subprocess.run") as mock_run,
    ):
        with pytest.raises(DockerSandboxRunnerError, match=r"timed out after 30s"):
            _run_docker_command(
                ["docker", "run", "--name", "sandbox-workspace-task-31", "image"],
                timeout=30,
            )

    mock_run.assert_called_once_with(
        ["docker", "kill", "sandbox-workspace-task-31"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_read_stream_bounded_preserves_partial_data_on_os_error() -> None:
    """Partial data captured before an OSError is returned rather than lost."""

    class BrokenStream:
        def __init__(self) -> None:
            self._calls = 0

        def read(self, n: int) -> bytes:
            self._calls += 1
            if self._calls == 1:
                return b"first chunk"
            raise OSError("pipe closed")

    buf = _read_stream_bounded(BrokenStream(), limit=1000)  # type: ignore[arg-type]
    assert buf == bytearray(b"first chunk")


def test_run_docker_command_cleans_up_threads_on_unexpected_exception() -> None:
    """Threads must be joined even when an unexpected exception occurs (e.g. KeyboardInterrupt)."""
    mock_proc = _make_popen_mock(stdout_data=b"output")

    # Make proc.wait raise KeyboardInterrupt on the first (non-timeout) call.
    interrupt_raised = False

    def _wait(**kwargs: object) -> int:
        nonlocal interrupt_raised
        if not interrupt_raised:
            interrupt_raised = True
            raise KeyboardInterrupt
        return 0

    mock_proc.wait.side_effect = _wait

    with patch("subprocess.Popen", return_value=mock_proc):
        with pytest.raises(KeyboardInterrupt):
            _run_docker_command(["docker", "run", "image"], timeout=30)

    # Verify that wait was called at least once (cleanup path executed).
    assert mock_proc.wait.call_count >= 1


def test_run_docker_command_closes_pipe_when_thread_hangs() -> None:
    """When a reader thread outlives _THREAD_JOIN_TIMEOUT the pipe is force-closed to unblock it."""
    import sandbox.runner as runner_module

    class HangingStream:
        def __init__(self) -> None:
            self.closed = False
            self.blocker = threading.Event()

        def read(self, n: int) -> bytes:
            if self.closed:
                raise ValueError("I/O operation on closed file")
            self.blocker.wait()
            return b""

        def close(self) -> None:
            self.closed = True
            self.blocker.set()

    stdout_pipe = HangingStream()
    stderr_pipe = io.BytesIO(b"")

    mock_proc = MagicMock()
    mock_proc.stdout = stdout_pipe
    mock_proc.stderr = stderr_pipe
    mock_proc.returncode = 0
    mock_proc.wait.return_value = 0

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch.object(runner_module, "_THREAD_JOIN_TIMEOUT", 0.05),
    ):
        result = _run_docker_command(["docker", "run", "image"], timeout=30)

    assert result.returncode == 0
    assert stdout_pipe.closed


def test_runner_redacts_secrets(tmp_path: Path) -> None:
    """The runner should redact known secrets from stdout, stderr, and logs."""
    request = DockerSandboxCommand(
        workspace=_workspace_handle(tmp_path, initialize_git=True),
        command=["echo", "my secret is password123"],
        secrets={"api_key": "password123"},
    )

    def fake_runner(
        command: list[str],
        *,
        timeout: int,
        redactor: SecretRedactor | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert redactor is not None
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="my secret is password123\n",
            stderr="error with password123\n",
        )

    runner = DockerSandboxRunner(command_runner=fake_runner)
    result = runner.run(request)

    assert result.stdout == "my secret is [REDACTED]\n"
    assert result.stderr == "error with [REDACTED]\n"

    artifact_paths = {
        artifact.name: request.workspace.workspace_path / artifact.uri
        for artifact in result.artifacts
    }
    assert artifact_paths["stdout.log"].read_text(encoding="utf-8") == "my secret is [REDACTED]\n"
    assert artifact_paths["stderr.log"].read_text(encoding="utf-8") == "error with [REDACTED]\n"


def test_build_docker_run_command_enforces_path_policy(tmp_path: Path) -> None:
    """The runner should reject requests whose working directory is denied by policy."""
    policy = PathPolicy(denied_prefixes=["/workspace/repo"])
    request = DockerSandboxCommand(
        workspace=_workspace_handle(tmp_path),
        command=["ls"],
        working_dir="/workspace/repo",
        path_policy=policy,
    )

    with pytest.raises(
        DockerSandboxRunnerError, match="Working directory is denied by path policy"
    ):
        _build_docker_run_command(request, image="alpine")
