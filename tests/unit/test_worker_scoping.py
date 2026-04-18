"""Unit tests for refined worker secret scoping."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sandbox import WorkspaceCleanupPolicy, WorkspaceHandle
from tools import (
    EXECUTE_BASH_TOOL_NAME,
    ToolCapabilityCategory,
    ToolDefinition,
    ToolExpectedArtifact,
    ToolPermissionLevel,
    ToolRegistry,
    ToolSideEffectLevel,
)
from workers.base import WorkerRequest, WorkerResult
from workers.codex_cli_worker import CodexCliWorker
from workers.gemini_cli_worker import GeminiCliWorker


def _create_mock_ws():
    return WorkspaceHandle(
        workspace_id="ws_123",
        task_id="task_123",
        repo_url="https://github.com/example/repo",
        workspace_path=Path("/tmp/repo"),
        repo_path=Path("/tmp/repo"),
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


def _create_mock_cont():
    mock_cont = MagicMock()
    mock_cont.container_name = "test-container"
    mock_cont.working_dir = "/tmp/repo"
    return mock_cont


def _create_mock_result():
    return WorkerResult(
        status="success",
        summary="Done",
        commands_run=[],
        files_changed=[],
        artifacts=[],
    )


def test_gemini_worker_honors_request_tools_scoping() -> None:
    """GeminiCliWorker should only inject secrets for the tools specified in the request."""
    tool_a = ToolDefinition(
        name="tool_a",
        description="Tool A",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.READ_ONLY,
        required_permission=ToolPermissionLevel.READ_ONLY,
        required_secrets=("SECRET_A",),
        timeout_seconds=60,
    )
    tool_b = ToolDefinition(
        name="tool_b",
        description="Tool B",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.READ_ONLY,
        required_permission=ToolPermissionLevel.READ_ONLY,
        required_secrets=("SECRET_B",),
        timeout_seconds=60,
    )
    tool_bash = ToolDefinition(
        name=EXECUTE_BASH_TOOL_NAME,
        description="Bash",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
        required_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        expected_artifacts=(ToolExpectedArtifact.CHANGED_FILES,),
        timeout_seconds=60,
    )
    registry = ToolRegistry(tools=(tool_a, tool_b, tool_bash))

    mock_adapter = MagicMock()
    worker = GeminiCliWorker(runtime_adapter=mock_adapter, tool_registry=registry)

    request = WorkerRequest(
        task_text="Run tool A",
        repo_url="https://github.com/example/repo",
        secrets={"SECRET_A": "val_a", "SECRET_B": "val_b", "OTHER": "val_other"},
        tools=["tool_a"],
    )

    with (
        patch.object(worker.workspace_manager, "create_workspace", return_value=_create_mock_ws()),
        patch.object(
            worker.container_manager, "start", return_value=_create_mock_cont()
        ) as mock_start_cont,
        patch.object(worker.container_manager, "stop"),
        patch.object(worker, "_session_factory"),
        patch("workers.gemini_cli_worker.run_cli_runtime_loop"),
        patch("workers.gemini_cli_worker.collect_changed_files", return_value=[]),
        patch("workers.gemini_cli_worker.collect_changed_files_from_repo_path", return_value=[]),
        patch(
            "workers.gemini_cli_worker._worker_result_from_execution",
            return_value=_create_mock_result(),
        ),
    ):
        worker._run_sync(request)

        mock_start_cont.assert_called_once()
        container_req = mock_start_cont.call_args[0][0]
        assert "SECRET_A" in container_req.environment
        assert "SECRET_B" not in container_req.environment
        assert "OTHER" not in container_req.environment


def test_codex_worker_honors_request_tools_scoping() -> None:
    """CodexCliWorker should only inject secrets for the tools specified in the request."""
    tool_a = ToolDefinition(
        name="tool_a",
        description="Tool A",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.READ_ONLY,
        required_permission=ToolPermissionLevel.READ_ONLY,
        required_secrets=("SECRET_A",),
        timeout_seconds=60,
    )
    tool_b = ToolDefinition(
        name="tool_b",
        description="Tool B",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.READ_ONLY,
        required_permission=ToolPermissionLevel.READ_ONLY,
        required_secrets=("SECRET_B",),
        timeout_seconds=60,
    )
    tool_bash = ToolDefinition(
        name=EXECUTE_BASH_TOOL_NAME,
        description="Bash",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
        required_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        expected_artifacts=(ToolExpectedArtifact.CHANGED_FILES,),
        timeout_seconds=60,
    )
    registry = ToolRegistry(tools=(tool_a, tool_b, tool_bash))

    mock_adapter = MagicMock()
    worker = CodexCliWorker(runtime_adapter=mock_adapter, tool_registry=registry)

    request = WorkerRequest(
        task_text="Run tool A",
        repo_url="https://github.com/example/repo",
        secrets={"SECRET_A": "val_a", "SECRET_B": "val_b", "OTHER": "val_other"},
        tools=["tool_a"],
    )

    with (
        patch.object(worker.workspace_manager, "create_workspace", return_value=_create_mock_ws()),
        patch.object(
            worker.container_manager, "start", return_value=_create_mock_cont()
        ) as mock_start_cont,
        patch.object(worker.container_manager, "stop"),
        patch.object(worker, "_session_factory"),
        patch("workers.codex_cli_worker.run_cli_runtime_loop"),
        patch("workers.codex_cli_worker.collect_changed_files", return_value=[]),
        patch("workers.codex_cli_worker.collect_changed_files_from_repo_path", return_value=[]),
        patch(
            "workers.codex_cli_worker._worker_result_from_execution",
            return_value=_create_mock_result(),
        ),
    ):
        worker._run_sync(request)

        mock_start_cont.assert_called_once()
        container_req = mock_start_cont.call_args[0][0]
        assert "SECRET_A" in container_req.environment
        assert "SECRET_B" not in container_req.environment
