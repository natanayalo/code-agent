"""Unit tests for RuntimeExecutor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sandbox import DockerSandboxContainer, WorkspaceHandle
from tools import (
    DEFAULT_TOOL_REGISTRY,
    EXECUTE_BASH_TOOL_NAME,
    ToolCapabilityCategory,
    ToolDefinition,
    ToolPermissionLevel,
    ToolRegistry,
    ToolSideEffectLevel,
)
from workers.base import ArtifactReference, WorkerRequest, WorkerResult
from workers.cli_runtime import (
    CliRuntimeAdapter,
    CliRuntimeBudgetLedger,
    CliRuntimeExecutionResult,
    CliRuntimeSettings,
)
from workers.runtime_executor import RuntimeExecutor
from workers.sandbox_adapter import SandboxSessionAdapter


def test_runtime_executor_execution_flow_success() -> None:
    """RuntimeExecutor runs loop, lint/format, self-review and returns success."""
    mock_adapter = MagicMock(spec=CliRuntimeAdapter)
    mock_sandbox_adapter = MagicMock(spec=SandboxSessionAdapter)

    mock_container = MagicMock(spec=DockerSandboxContainer)
    mock_container.working_dir = "/workspace"
    mock_session = MagicMock()

    mock_sandbox_adapter.session_context.return_value.__enter__.return_value = (
        mock_container,
        mock_session,
    )

    # Mock settings and registry
    settings = CliRuntimeSettings()
    registry = DEFAULT_TOOL_REGISTRY

    executor = RuntimeExecutor(
        runtime_adapter=mock_adapter,
        tool_registry=registry,
        sandbox_adapter=mock_sandbox_adapter,
        runtime_settings=settings,
    )

    workspace = MagicMock(spec=WorkspaceHandle)
    workspace.workspace_id = "ws_123"
    workspace.workspace_path = MagicMock()
    workspace.workspace_path.as_uri.return_value = "file:///tmp/repo"
    workspace.repo_path = Path("/tmp/repo")

    request = WorkerRequest(
        task_text="Test execution",
        repo_url="https://example.com/repo",
    )

    mock_exec_result = CliRuntimeExecutionResult(
        status="success",
        summary="success",
        stop_reason="final_answer",
        budget_ledger=CliRuntimeBudgetLedger(max_iterations=10),
    )

    with (
        patch(
            "workers.runtime_executor.run_cli_runtime_loop", return_value=mock_exec_result
        ) as mock_run_loop,
        patch(
            "workers.runtime_executor.collect_changed_files_and_apply_post_run_lint_format",
            return_value=(
                ["file.py"],
                {"ran": True},
                [ArtifactReference(name="lint", uri="file:///tmp/lint", artifact_type="log")],
            ),
        ) as mock_lint,
        patch(
            "workers.runtime_executor.run_shared_self_review_fix_loop",
            return_value=(
                None,
                ["file.py"],
                {"ran": True},
                [ArtifactReference(name="lint", uri="file:///tmp/lint", artifact_type="log")],
            ),
        ) as mock_review,
        patch("workers.runtime_executor.collect_diff_for_review", return_value="diff text"),
    ):
        result = executor.execute(request, workspace=workspace)

        assert isinstance(result, WorkerResult)
        assert result.status == "success"
        assert result.files_changed == ["file.py"]
        assert result.diff_text == "diff text"

        mock_run_loop.assert_called_once()
        mock_lint.assert_called_once()
        mock_review.assert_called_once()


def test_runtime_executor_auto_enables_network() -> None:
    """RuntimeExecutor enables network when tool requires it and permission allows."""
    mock_adapter = MagicMock(spec=CliRuntimeAdapter)
    mock_sandbox_adapter = MagicMock(spec=SandboxSessionAdapter)

    # Use a custom tool registry with a tool requiring network and the execute_bash tool
    network_tool = ToolDefinition(
        name="network_tool",
        description="A tool requiring network access",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.READ_ONLY,
        required_permission=ToolPermissionLevel.NETWORKED_WRITE,
        network_required=True,
        timeout_seconds=60,
    )
    bash_tool = ToolDefinition(
        name=EXECUTE_BASH_TOOL_NAME,
        description="Bash",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
        required_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        timeout_seconds=60,
    )
    registry = ToolRegistry(tools=(network_tool, bash_tool))

    executor = RuntimeExecutor(
        runtime_adapter=mock_adapter,
        tool_registry=registry,
        sandbox_adapter=mock_sandbox_adapter,
        runtime_settings=CliRuntimeSettings(),
    )

    request = WorkerRequest(
        task_text="Run test",
        repo_url="https://example.com/repo",
        tools=["network_tool"],
        constraints={"granted_permission": "networked_write"},
    )

    workspace = MagicMock(spec=WorkspaceHandle)
    workspace.workspace_id = "ws_123"
    workspace.workspace_path = MagicMock()
    workspace.workspace_path.as_uri.return_value = "file:///tmp/repo"
    workspace.repo_path = Path("/tmp/repo")

    mock_exec_result = CliRuntimeExecutionResult(
        status="success",
        summary="success",
        stop_reason="final_answer",
        budget_ledger=CliRuntimeBudgetLedger(max_iterations=10),
    )

    # Mock context manager
    class MockContextManager:
        def __enter__(self):
            return MagicMock(), MagicMock()

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_sandbox_adapter.session_context.return_value = MockContextManager()

    with (
        patch("workers.runtime_executor.run_cli_runtime_loop", return_value=mock_exec_result),
        patch(
            "workers.runtime_executor.collect_changed_files_and_apply_post_run_lint_format",
            return_value=([], {}, []),
        ),
        patch(
            "workers.runtime_executor.run_shared_self_review_fix_loop",
            return_value=(None, [], {}, []),
        ),
        patch("workers.runtime_executor.collect_diff_for_review", return_value=""),
    ):
        executor.execute(request, workspace=workspace)

        # Check that session_context was called with network_enabled=True
        mock_sandbox_adapter.session_context.assert_called_once()
        kwargs = mock_sandbox_adapter.session_context.call_args[1]
        assert kwargs["network_enabled"] is True


def test_runtime_executor_execution_cancellation() -> None:
    """RuntimeExecutor maps cancellation token trigger to timeout error result."""
    mock_adapter = MagicMock(spec=CliRuntimeAdapter)
    mock_sandbox_adapter = MagicMock(spec=SandboxSessionAdapter)

    executor = RuntimeExecutor(
        runtime_adapter=mock_adapter,
        tool_registry=DEFAULT_TOOL_REGISTRY,
        sandbox_adapter=mock_sandbox_adapter,
        runtime_settings=CliRuntimeSettings(),
    )

    workspace = MagicMock(spec=WorkspaceHandle)
    workspace.workspace_id = "ws_123"
    workspace.workspace_path = MagicMock()
    workspace.workspace_path.as_uri.return_value = "file:///tmp/repo"
    workspace.repo_path = Path("/tmp/repo")

    request = WorkerRequest(
        task_text="Test cancellation",
        repo_url="https://example.com/repo",
    )

    mock_exec_result = CliRuntimeExecutionResult(
        status="failure",
        summary="failed",
        stop_reason="worker_timeout",
        budget_ledger=CliRuntimeBudgetLedger(max_iterations=10),
    )

    class MockContextManager:
        def __enter__(self):
            return MagicMock(), MagicMock()

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_sandbox_adapter.session_context.return_value = MockContextManager()

    with (
        patch("workers.runtime_executor.run_cli_runtime_loop", return_value=mock_exec_result),
        patch(
            "workers.runtime_executor.collect_changed_files_and_apply_post_run_lint_format",
            return_value=([], {}, []),
        ),
        patch("workers.runtime_executor.collect_diff_for_review", return_value=""),
    ):
        # Pass a cancellation token that always returns True
        result = executor.execute(request, workspace=workspace, cancel_token=lambda: True)

        assert isinstance(result, WorkerResult)
        assert result.status == "error"
        assert result.failure_kind == "timeout"
        assert "cancelled" in (result.summary or "").lower()
