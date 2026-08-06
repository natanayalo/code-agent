"""Unit tests for native worker helper functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle
from workers import ArtifactReference, WorkerRequest, WorkerResult
from workers.antigravity_cli_worker_native import (
    _migrated_mcp_config,
)
from workers.cli_runtime_types import CliRuntimeExecutionResult
from workers.codex_cli_worker_native import (
    _apply_cleanup_outcome as codex_apply_cleanup_outcome,
)
from workers.codex_cli_worker_native import (
    _next_action_hint as codex_next_action_hint,
)
from workers.codex_cli_worker_native import (
    _slugify as codex_slugify,
)
from workers.codex_cli_worker_native import (
    _workspace_artifacts as codex_workspace_artifacts,
)
from workers.codex_cli_worker_native import (
    _workspace_task_id as codex_workspace_task_id,
)
from workers.gemini_cli_worker_native import (
    _apply_cleanup_outcome as gemini_apply_cleanup_outcome,
)
from workers.gemini_cli_worker_native import (
    _next_action_hint as gemini_next_action_hint,
)
from workers.gemini_cli_worker_native import (
    _slugify as gemini_slugify,
)
from workers.gemini_cli_worker_native import (
    _workspace_artifacts as gemini_workspace_artifacts,
)
from workers.gemini_cli_worker_native import (
    _workspace_task_id as gemini_workspace_task_id,
)


def test_slugify():
    assert codex_slugify("My Special Task!") == "my-special-task"
    assert gemini_slugify("   ") == "task"


def test_workspace_task_id():
    req_with_id = WorkerRequest(task_id="custom-id", task_text="text")
    assert codex_workspace_task_id(req_with_id) == "custom-id"

    req_with_session = WorkerRequest(session_id="session-123", task_text="text")
    assert codex_workspace_task_id(req_with_session) == "codex-cli-session-123"
    assert gemini_workspace_task_id(req_with_session) == "gemini-cli-session-123"


def test_workspace_artifacts():
    ws_path = Path("/tmp/test_ws")
    handle = WorkspaceHandle(
        workspace_id="ws1",
        workspace_path=ws_path,
        task_id="t1",
        repo_path=ws_path,
        repo_url="http://repo",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )

    arts = codex_workspace_artifacts(handle)
    assert len(arts) == 1
    assert arts[0].artifact_type == "workspace"
    assert arts[0].uri == ws_path.as_uri()

    arts_g = gemini_workspace_artifacts(handle)
    assert len(arts_g) == 1


def test_migrated_mcp_config(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"mcpServers": {"server1": {"url": "http://localhost:8080"}}}')

    migrated = _migrated_mcp_config(settings)
    assert migrated is not None
    assert "server1" in migrated["mcpServers"]
    assert migrated["mcpServers"]["server1"]["serverUrl"] == "http://localhost:8080"


def test_apply_cleanup_outcome():
    result = WorkerResult(
        status="success",
        summary="Done.",
        artifacts=[ArtifactReference(name="ws", uri="file:///tmp", artifact_type="workspace")],
        next_action_hint="hint",
    )

    # If not deleted, returned unchanged
    assert codex_apply_cleanup_outcome(result, workspace_deleted=False) == result

    # If deleted, summary updated, artifacts cleared, next_action_hint cleared
    cleaned = codex_apply_cleanup_outcome(result, workspace_deleted=True)
    assert "Workspace cleaned up" in cleaned.summary
    assert cleaned.artifacts == []
    assert cleaned.next_action_hint is None

    cleaned_g = gemini_apply_cleanup_outcome(result, workspace_deleted=True)
    assert "Workspace cleaned up" in cleaned_g.summary


def test_next_action_hint():
    res_perm = MagicMock(spec=CliRuntimeExecutionResult)
    res_perm.stop_reason = "permission_required"
    assert codex_next_action_hint(res_perm) == "request_higher_permission"
    assert gemini_next_action_hint(res_perm) == "request_higher_permission"

    res_timeout = MagicMock(spec=CliRuntimeExecutionResult)
    res_timeout.stop_reason = "worker_timeout"
    assert codex_next_action_hint(res_timeout) == "increase_budget_or_reduce_scope"
    assert gemini_next_action_hint(res_timeout) == "increase_budget_or_reduce_scope"

    res_context = MagicMock(spec=CliRuntimeExecutionResult)
    res_context.stop_reason = "context_window"
    assert codex_next_action_hint(res_context) == "reduce_context_or_scope"
    assert gemini_next_action_hint(res_context) == "reduce_context_or_scope"

    res_other = MagicMock(spec=CliRuntimeExecutionResult)
    res_other.stop_reason = "completed"
    assert codex_next_action_hint(res_other) == "inspect_workspace_artifacts"
