"""Unit tests for shell permission-policy helpers."""

from __future__ import annotations

from tools import (
    DEFAULT_TOOL_REGISTRY,
    ToolPermissionLevel,
    granted_permission_from_constraints,
    permission_allows,
    resolve_bash_command_permission,
)


def test_granted_permission_from_constraints_defaults_to_workspace_write() -> None:
    """Worker runs should default to workspace-write authority when nothing narrower is given."""
    assert granted_permission_from_constraints({}) == ToolPermissionLevel.WORKSPACE_WRITE


def test_granted_permission_from_constraints_parses_known_strings() -> None:
    """Constraint values should accept the explicit permission strings we expose."""
    resolved = granted_permission_from_constraints({"granted_permission": "dangerous_shell"})

    assert resolved == ToolPermissionLevel.DANGEROUS_SHELL


def test_permission_allows_respects_permission_ordering() -> None:
    """Higher permission levels should satisfy lower-level requests."""
    assert permission_allows(
        ToolPermissionLevel.DANGEROUS_SHELL,
        ToolPermissionLevel.WORKSPACE_WRITE,
    )
    assert not permission_allows(
        ToolPermissionLevel.WORKSPACE_WRITE,
        ToolPermissionLevel.NETWORKED_WRITE,
    )


def test_resolve_bash_command_permission_classifies_read_only_commands() -> None:
    """Read-only shell commands should not require workspace-write access."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "git status --short",
        tool,
        granted_permission=ToolPermissionLevel.READ_ONLY,
    )

    assert decision.required_permission == ToolPermissionLevel.READ_ONLY
    assert decision.allowed is True


def test_resolve_bash_command_permission_escalates_for_dangerous_shell_commands() -> None:
    """Dangerous shell commands should require explicit dangerous-shell permission."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "rm -rf build",
        tool,
        granted_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.DANGEROUS_SHELL
    assert decision.allowed is False


def test_resolve_bash_command_permission_escalates_for_push_and_deploy_commands() -> None:
    """Push and deploy actions should sit at the top of the permission ladder."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    decision = resolve_bash_command_permission(
        "git push origin HEAD",
        tool,
        granted_permission=ToolPermissionLevel.NETWORKED_WRITE,
    )

    assert decision.required_permission == ToolPermissionLevel.GIT_PUSH_OR_DEPLOY
    assert decision.allowed is False
