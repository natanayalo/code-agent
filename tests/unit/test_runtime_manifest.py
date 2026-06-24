"""Unit tests for runtime operating contract manifests."""

from __future__ import annotations

from typing import get_args

from pytest import MonkeyPatch

from orchestrator.runtime_manifest import build_runtime_manifest
from orchestrator.state import TaskSpec
from tools.registry import SEARCH_FILE_TOOL_NAME, VIEW_FILE_TOOL_NAME, ToolDefinition, ToolRegistry
from workers.base import MaintenanceActionType


def test_build_runtime_manifest_projects_task_and_worker_context() -> None:
    """Runtime manifests should summarize task policy, tools, and selected runtime."""
    spec = TaskSpec(
        goal="Implement a narrow slice",
        risk_level="high",
        allowed_actions=["read_repo_files", "modify_workspace_files"],
        forbidden_actions=["hardcode_secrets", "deploy_or_merge_without_approval"],
        requires_permission=True,
        delivery_mode="draft_pr",
    )

    manifest = build_runtime_manifest(
        default_image="test-image",
        workspace_root="/tmp/workspaces",
        worker_type="codex",
        worker_profile="codex-native-executor",
        runtime_mode="native_agent",
        workspace_id="workspace-1",
        task_spec=spec,
        read_only=False,
        network_enabled=True,
        budget={"max_minutes": 15},
        requested_tools=[VIEW_FILE_TOOL_NAME, SEARCH_FILE_TOOL_NAME, "missing-tool"],
    )

    assert manifest.sandbox.default_image == "test-image"
    assert manifest.sandbox.workspace_root == "/tmp/workspaces"
    assert manifest.worker.worker_type == "codex"
    assert manifest.worker.worker_profile == "codex-native-executor"
    assert manifest.worker.runtime_mode == "native_agent"
    assert manifest.worker.workspace_id == "workspace-1"
    assert manifest.task.delivery_mode == "draft_pr"
    assert manifest.task.budget == {"max_minutes": 15}
    assert manifest.task.approval_required is True
    assert manifest.task.forbidden_actions == [
        "hardcode_secrets",
        "deploy_or_merge_without_approval",
    ]
    assert [tool.name for tool in manifest.tools] == [VIEW_FILE_TOOL_NAME, SEARCH_FILE_TOOL_NAME]


def test_build_runtime_manifest_accepts_raw_string_tool_metadata() -> None:
    """Tool metadata may be plain strings when built from non-validated definitions."""
    tool = ToolDefinition.model_construct(
        name="custom_tool",
        description="A custom tool",
        capability_category="shell",
        side_effect_level="read_only",
        required_permission="read_only",
        timeout_seconds=5,
        network_required=False,
        deterministic=True,
    )
    registry = ToolRegistry.model_construct(tools=(tool,))

    manifest = build_runtime_manifest(
        default_image="image",
        workspace_root="/tmp/workspaces",
        tool_registry=registry,
    )

    assert manifest.tools[0].capability_category == "shell"
    assert manifest.tools[0].side_effect_level == "read_only"
    assert manifest.tools[0].required_permission == "read_only"


def test_build_runtime_manifest_resolves_service_identity_from_env(
    monkeypatch: MonkeyPatch,
) -> None:
    """Runtime manifest service identity should reflect deployed environment metadata."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CODE_AGENT_ENV", "staging")
    monkeypatch.setenv("BUILD_SHA", "build-123")
    monkeypatch.setenv("COMMIT_SHA", "commit-456")

    manifest = build_runtime_manifest(default_image="image", workspace_root="/tmp/workspaces")

    assert manifest.service.environment == "production"
    assert manifest.service.build_sha == "build-123"


def test_build_runtime_manifest_service_identity_allows_explicit_overrides(
    monkeypatch: MonkeyPatch,
) -> None:
    """Explicit service identity fields should win over process environment fallback."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BUILD_SHA", "build-123")

    manifest = build_runtime_manifest(
        default_image="image",
        workspace_root="/tmp/workspaces",
        environment="local-dev",
        build_sha="manual-sha",
    )

    assert manifest.service.environment == "local-dev"
    assert manifest.service.build_sha == "manual-sha"


def test_build_runtime_manifest_is_request_only_for_maintenance_actions() -> None:
    """Maintenance actions advertised to agents must remain request-only."""
    manifest = build_runtime_manifest(default_image="image", workspace_root="/tmp/workspaces")

    assert {action.action for action in manifest.maintenance_actions} == set(
        get_args(MaintenanceActionType)
    )
    assert all(action.request_only for action in manifest.maintenance_actions)
    assert all(action.requires_operator_approval for action in manifest.maintenance_actions)
