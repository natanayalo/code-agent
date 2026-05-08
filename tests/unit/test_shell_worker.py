from unittest.mock import MagicMock, patch

import pytest

from db.enums import WorkerRuntimeMode
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle
from workers.base import WorkerRequest
from workers.native_agent_models import NativeAgentRunResult
from workers.shell_worker import ShellWorker


@pytest.fixture
def mock_workspace_manager():
    return MagicMock()


@pytest.fixture
def mock_container_manager():
    return MagicMock()


@pytest.fixture
def shell_worker(mock_workspace_manager, mock_container_manager):
    return ShellWorker(
        workspace_manager=mock_workspace_manager,
        container_manager=mock_container_manager,
    )


@pytest.fixture
def workspace_handle():
    return WorkspaceHandle(
        workspace_id="test-workspace",
        task_id="test-task",
        workspace_path="/tmp/workspace",
        repo_path="/tmp/repo",
        repo_url="https://example.com/repo.git",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


@pytest.mark.anyio
async def test_shell_worker_run_success(
    shell_worker, mock_workspace_manager, mock_container_manager, workspace_handle
):
    request = WorkerRequest(
        session_id="test-session",
        repo_url="https://example.com/repo.git",
        branch="main",
        task_text="echo hello",
        runtime_mode=WorkerRuntimeMode.SHELL,
        budget={"worker_timeout_seconds": 60},
    )

    mock_workspace_manager.create_workspace.return_value = workspace_handle

    mock_container = MagicMock()
    mock_container.container_name = "test-container"
    mock_container_manager.start.return_value = mock_container

    native_result = NativeAgentRunResult(
        status="success",
        summary="Command passed.",
        command="echo hello",
        exit_code=0,
        duration_seconds=1.0,
        timed_out=False,
    )

    with patch("workers.shell_worker.run_native_agent", return_value=native_result) as mock_run:
        result = await shell_worker.run(request)

        assert result.status == "success"
        assert result.summary == "Command passed."
        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        assert args[0].command == ["docker", "exec", "-i", "test-container", "/bin/sh", "-e"]
        assert args[0].prompt == "echo hello"


@pytest.mark.anyio
async def test_shell_worker_applies_diff(
    shell_worker, mock_workspace_manager, mock_container_manager, workspace_handle
):
    request = WorkerRequest(
        session_id="test-session",
        repo_url="https://example.com/repo.git",
        branch="main",
        task_text="pytest",
        runtime_mode=WorkerRuntimeMode.SHELL,
        constraints={"apply_diff_text": "diff content"},
    )

    mock_workspace_manager.create_workspace.return_value = workspace_handle
    mock_container = MagicMock()
    mock_container.container_name = "test-container"
    mock_container_manager.start.return_value = mock_container

    apply_result = NativeAgentRunResult(
        status="success",
        summary="Applied.",
        command="git apply",
        exit_code=0,
        duration_seconds=1.0,
        timed_out=False,
    )
    native_result = NativeAgentRunResult(
        status="success",
        summary="Tests passed.",
        command="pytest",
        exit_code=0,
        duration_seconds=1.0,
        timed_out=False,
    )

    with patch(
        "workers.shell_worker.run_native_agent", side_effect=[apply_result, native_result]
    ) as mock_run:
        result = await shell_worker.run(request)

        assert result.status == "success"
        assert mock_run.call_count == 2

        # First call should be git apply
        first_call_args = mock_run.call_args_list[0][0][0]
        assert "git" in first_call_args.command
        assert "apply" in first_call_args.command
        assert first_call_args.prompt == "diff content"


@pytest.mark.anyio
async def test_shell_worker_handles_apply_failure(
    shell_worker, mock_workspace_manager, mock_container_manager, workspace_handle
):
    request = WorkerRequest(
        session_id="test-session",
        repo_url="https://example.com/repo.git",
        branch="main",
        task_text="pytest",
        runtime_mode=WorkerRuntimeMode.SHELL,
        constraints={"apply_diff_text": "bad diff"},
    )

    mock_workspace_manager.create_workspace.return_value = workspace_handle
    mock_container = MagicMock()
    mock_container.container_name = "test-container"
    mock_container_manager.start.return_value = mock_container

    apply_result = NativeAgentRunResult(
        status="error",
        summary="Apply failed.",
        command="git apply",
        exit_code=1,
        duration_seconds=1.0,
        timed_out=False,
    )

    with patch("workers.shell_worker.run_native_agent", return_value=apply_result) as mock_run:
        result = await shell_worker.run(request)

        assert result.status == "error"
        assert "Failed to apply changes" in result.summary
        assert mock_run.call_count == 1


@pytest.mark.anyio
async def test_shell_worker_handles_cancellation(
    shell_worker, mock_workspace_manager, mock_container_manager, workspace_handle
):
    request = WorkerRequest(
        session_id="test-session",
        repo_url="https://example.com/repo.git",
        branch="main",
        task_text="echo hello",
        runtime_mode=WorkerRuntimeMode.SHELL,
        budget={"worker_timeout_seconds": 60},
    )

    mock_workspace_manager.create_workspace.return_value = workspace_handle
    mock_container = MagicMock()
    mock_container.container_name = "test-container"
    mock_container_manager.start.return_value = mock_container

    # Patch run_sync_with_cancellable_executor to inject cancel=True
    async def mock_run_sync(fn):
        return fn(lambda: True)

    with patch(
        "workers.shell_worker.run_sync_with_cancellable_executor", side_effect=mock_run_sync
    ):
        result = await shell_worker.run(request)

        assert result.status == "error"
        assert result.failure_kind == "timeout"
        assert "cancelled" in result.summary.lower()
