"""Permission policy helpers for tool execution."""

from __future__ import annotations

import re
import shlex
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

_TOKEN_PREFIX_PERMISSION_RULES: tuple[
    tuple[ToolPermissionLevel, str, tuple[tuple[str, ...], ...]],
    ...,
] = (
    (
        ToolPermissionLevel.GIT_PUSH_OR_DEPLOY,
        "Command pushes changes or performs a deploy-like action.",
        (
            ("git", "push"),
            ("kubectl", "apply"),
            ("terraform", "apply"),
            ("fly", "deploy"),
            ("flyctl", "deploy"),
            ("deploy",),
            ("release",),
        ),
    ),
    (
        ToolPermissionLevel.NETWORKED_WRITE,
        "Command writes state while depending on network access.",
        (
            ("curl",),
            ("wget",),
            ("pip", "install"),
            ("uv", "pip", "install"),
            ("npm", "install"),
            ("npm", "add"),
            ("pnpm", "install"),
            ("pnpm", "add"),
            ("yarn", "install"),
            ("yarn", "add"),
            ("brew", "install"),
            ("git", "clone"),
            ("git", "pull"),
        ),
    ),
    (
        ToolPermissionLevel.DANGEROUS_SHELL,
        "Command performs a destructive shell or git operation.",
        (
            ("rm",),
            ("git", "clean"),
            ("git", "reset"),
            ("mkfs",),
            ("dd",),
        ),
    ),
)

_SHELL_OPERATOR_TOKENS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"})
_TOKEN_WITH_SHELL_OPERATOR_PATTERN = re.compile(r"[|&;<>]")
_DANGEROUS_TOKEN_PATTERNS = (
    re.compile(r"\bdrop\s+(?:database|table)\b"),
    re.compile(r"\btruncate\s+table\b"),
)
_SAFE_READ_ONLY_COMMANDS = frozenset({"cat", "head", "ls", "pwd", "tail", "wc"})
_SAFE_RG_BLOCKLIST = frozenset({"--pre", "--pre-glob"})


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


def _command_tokens(command: str) -> tuple[str, ...] | None:
    """Parse shell-like command tokens when the input is well formed."""
    try:
        return tuple(shlex.split(command, posix=True))
    except ValueError:
        return None


def _has_shell_control_operators(command: str, tokens: tuple[str, ...]) -> bool:
    """Return whether the command uses shell features that break read-only proofs."""
    if "$(" in command or "`" in command or "\n" in command or "\r" in command:
        return True
    return any(
        token in _SHELL_OPERATOR_TOKENS or _TOKEN_WITH_SHELL_OPERATOR_PATTERN.search(token)
        for token in tokens
    )


def _matches_token_prefix(tokens: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    """Return whether a tokenized command starts with the given normalized prefix."""
    return len(tokens) >= len(prefix) and tokens[: len(prefix)] == prefix


def _classify_token_prefix_permission(
    tokens: tuple[str, ...],
) -> tuple[ToolPermissionLevel, str] | None:
    """Resolve command permissions from normalized executable/subcommand prefixes."""
    for permission_level, reason, prefixes in _TOKEN_PREFIX_PERMISSION_RULES:
        if any(_matches_token_prefix(tokens, prefix) for prefix in prefixes):
            return permission_level, reason
    return None


def _contains_dangerous_token_pattern(tokens: tuple[str, ...]) -> bool:
    """Return whether any normalized token embeds a dangerous SQL-style phrase."""
    return any(pattern.search(token) for token in tokens for pattern in _DANGEROUS_TOKEN_PATTERNS)


def _is_safe_rg_command(tokens: tuple[str, ...]) -> bool:
    """Allow plain ripgrep searches while rejecting flags that spawn helpers."""
    return not any(
        token in _SAFE_RG_BLOCKLIST
        or any(token.startswith(f"{flag}=") for flag in _SAFE_RG_BLOCKLIST)
        for token in tokens[1:]
    )


def _is_safe_read_only_command(
    command: str,
    *,
    tokens: tuple[str, ...],
    normalized_tokens: tuple[str, ...],
) -> bool:
    """Return whether a command matches the narrow read-only allowlist."""
    if _has_shell_control_operators(command, tokens):
        return False

    executable = normalized_tokens[0]
    if executable in _SAFE_READ_ONLY_COMMANDS:
        return executable in {"ls", "pwd"} or len(normalized_tokens) > 1
    if executable == "git":
        return len(normalized_tokens) > 1 and normalized_tokens[1] == "status"
    if executable == "rg":
        return _is_safe_rg_command(normalized_tokens)
    return False


def _classify_bash_command_permission(
    command: str,
    *,
    default_permission: ToolPermissionLevel,
) -> tuple[ToolPermissionLevel, str]:
    """Return the required permission level for a bash command."""
    tokens = _command_tokens(command)
    if not tokens:
        return (
            default_permission,
            f"Command uses the tool's default permission level ({default_permission.value}).",
        )

    normalized_tokens = tuple(token.lower() for token in tokens)
    token_prefix_decision = _classify_token_prefix_permission(normalized_tokens)
    if token_prefix_decision is not None:
        return token_prefix_decision
    if _contains_dangerous_token_pattern(normalized_tokens):
        return (
            ToolPermissionLevel.DANGEROUS_SHELL,
            "Command performs a destructive shell or git operation.",
        )
    if _is_safe_read_only_command(
        command,
        tokens=tokens,
        normalized_tokens=normalized_tokens,
    ):
        return ToolPermissionLevel.READ_ONLY, "Command matches the narrow read-only allowlist."
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
