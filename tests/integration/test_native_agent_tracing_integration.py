"""Integration tests for native agent tracing propagation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from db.enums import WorkerRuntimeMode
from sandbox import WorkspaceCleanupPolicy, WorkspaceHandle
from workers.base import WorkerRequest
from workers.codex_cli_worker import CodexCliWorker


@pytest.fixture
def mock_runner():
    with (
        patch("workers.codex_cli_worker.run_native_agent") as mock_run,
        patch("workers.codex_cli_worker.format_native_run_summary", return_value="done"),
    ):
        result = MagicMock()
        result.status = "success"
        result.summary = "done"
        result.command = "codex exec -C /tmp/repo - "
        result.exit_code = 0
        result.duration_seconds = 1.0
        result.timed_out = False
        result.files_changed = []
        result.artifacts = []
        result.diff_text = ""
        mock_run.return_value = result
        yield mock_run


@pytest.mark.anyio
async def test_codex_worker_propagates_redactor_to_native_runner(
    tmp_path: Path, mock_runner
) -> None:
    """Codex worker should instantiate a redactor with request secrets and pass it to the runner."""
    adapter = MagicMock()
    worker = CodexCliWorker(
        runtime_adapter=adapter,
        default_runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        native_sandbox_mode="off",
    )

    # Use a real WorkspaceHandle to avoid Pydantic validation errors
    workspace = WorkspaceHandle(
        workspace_id="test-workspace",
        task_id="test-task",
        workspace_path=tmp_path / "workspace",
        repo_path=tmp_path / "repo",
        repo_url="https://github.com/org/repo",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )
    workspace.repo_path.mkdir(parents=True)

    request = WorkerRequest(
        session_id="test-session",
        repo_url="https://github.com/org/repo",
        branch="main",
        task_text="do stuff",
        secrets={"API_KEY": "secret-123", "DB_PASS": "secret-456"},
    )

    with patch.object(worker, "_provision_workspace", return_value=workspace):
        with patch.object(worker, "_cleanup_workspace", return_value=True):
            await worker.run(request)

    # Check the call to run_native_agent
    mock_runner.assert_called_once()
    run_request = mock_runner.call_args[0][0]

    assert run_request.redactor is not None
    # Check that secrets are redacted
    assert run_request.redactor.redact("my secret-123 is here") == "my [REDACTED] is here"
    assert run_request.redactor.redact("pass is secret-456") == "pass is [REDACTED]"
