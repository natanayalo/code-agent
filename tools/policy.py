"""Permission policy helpers for tool execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from tools.registry import ToolDefinition, ToolModel, ToolPermissionLevel

_PERMISSION_ORDER = {
    ToolPermissionLevel.READ_ONLY: 0,
    ToolPermissionLevel.WORKSPACE_WRITE: 1,
    ToolPermissionLevel.DANGEROUS_SHELL: 2,
    ToolPermissionLevel.NETWORKED_WRITE: 3,
    ToolPermissionLevel.GIT_PUSH_OR_DEPLOY: 4,
}

_COMMAND_PERMISSION_RULES: tuple[
    tuple[ToolPermissionLevel, str, tuple[re.Pattern[str], ...]],
    ...,
] = (
    (
        ToolPermissionLevel.GIT_PUSH_OR_DEPLOY,
        "Command pushes changes or performs a deploy-like action.",
        (
            re.compile(r"\bgit\s+push\b"),
            re.compile(r"\bkubectl\s+apply\b"),
            re.compile(r"\bterraform\s+apply\b"),
            re.compile(r"\b(?:fly|flyctl)\s+deploy\b"),
            re.compile(r"\bdeploy\b"),
            re.compile(r"\brelease\b"),
        ),
    ),
    (
        ToolPermissionLevel.NETWORKED_WRITE,
        "Command writes state while depending on network access.",
        (
            re.compile(r"\bcurl\b"),
            re.compile(r"\bwget\b"),
            re.compile(r"\bpip\s+install\b"),
            re.compile(r"\buv\s+pip\s+install\b"),
            re.compile(r"\bnpm\s+(?:install|add)\b"),
            re.compile(r"\bpnpm\s+(?:install|add)\b"),
            re.compile(r"\byarn\s+(?:install|add)\b"),
            re.compile(r"\bbrew\s+install\b"),
            re.compile(r"\bgit\s+(?:clone|pull)\b"),
        ),
    ),
    (
        ToolPermissionLevel.DANGEROUS_SHELL,
        "Command performs a destructive shell or git operation.",
        (
            re.compile(r"\brm\b"),
            re.compile(r"\bgit\s+clean\b"),
            re.compile(r"\bgit\s+reset\b"),
            re.compile(r"\bdrop\s+(?:database|table)\b"),
            re.compile(r"\btruncate\s+table\b"),
            re.compile(r"\bmkfs\b"),
            re.compile(r"\bdd\b"),
        ),
    ),
    (
        ToolPermissionLevel.READ_ONLY,
        "Command appears read-only.",
        (
            re.compile(r"^\s*cat\b"),
            re.compile(r"^\s*head\b"),
            re.compile(r"^\s*tail\b"),
            re.compile(r"^\s*ls\b"),
            re.compile(r"^\s*pwd\b"),
            re.compile(r"^\s*find\b"),
            re.compile(r"^\s*wc\b"),
            re.compile(r"^\s*rg\b"),
            re.compile(r"^\s*sed\s+-n\b"),
            re.compile(r"^\s*git\s+(?:status|diff|show|log)\b"),
        ),
    ),
)


class ToolPermissionDecision(ToolModel):
    """Resolved permission requirement for a concrete tool invocation."""

    tool_name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    granted_permission: ToolPermissionLevel
    required_permission: ToolPermissionLevel
    allowed: bool
    reason: str = Field(min_length=1)


def permission_rank(level: ToolPermissionLevel) -> int:
    """Return a stable rank for permission comparisons."""
    return _PERMISSION_ORDER[level]


def permission_allows(
    granted_permission: ToolPermissionLevel,
    required_permission: ToolPermissionLevel,
) -> bool:
    """Return whether a granted permission level satisfies the requirement."""
    return permission_rank(granted_permission) >= permission_rank(required_permission)


def _coerce_permission_level(value: object) -> ToolPermissionLevel | None:
    """Parse a permission level from strings or enum values."""
    if isinstance(value, ToolPermissionLevel):
        return value
    if isinstance(value, str):
        normalized_value = value.strip().lower()
        if not normalized_value:
            return None
        try:
            return ToolPermissionLevel(normalized_value)
        except ValueError:
            return None
    return None


def granted_permission_from_constraints(
    constraints: Mapping[str, Any],
    *,
    default: ToolPermissionLevel = ToolPermissionLevel.WORKSPACE_WRITE,
) -> ToolPermissionLevel:
    """Resolve the currently granted permission level from worker constraints."""
    for key in ("granted_permission", "allowed_permission_level", "permission_level"):
        resolved = _coerce_permission_level(constraints.get(key))
        if resolved is not None:
            return resolved
    return default


def _classify_bash_command_permission(
    command: str,
    *,
    default_permission: ToolPermissionLevel,
) -> tuple[ToolPermissionLevel, str]:
    """Return the required permission level for a bash command."""
    normalized_command = " ".join(command.lower().split())
    for permission_level, reason, patterns in _COMMAND_PERMISSION_RULES:
        if any(pattern.search(normalized_command) for pattern in patterns):
            return permission_level, reason
    return (
        default_permission,
        f"Command uses the tool's default permission level ({default_permission.value}).",
    )


def resolve_bash_command_permission(
    command: str,
    tool: ToolDefinition,
    *,
    granted_permission: ToolPermissionLevel,
) -> ToolPermissionDecision:
    """Resolve whether a concrete bash command is allowed under the granted permission."""
    required_permission, reason = _classify_bash_command_permission(
        command,
        default_permission=tool.required_permission,
    )
    return ToolPermissionDecision(
        tool_name=tool.name,
        command=command,
        granted_permission=granted_permission,
        required_permission=required_permission,
        allowed=permission_allows(granted_permission, required_permission),
        reason=reason,
    )
