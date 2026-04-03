"""Tool integration package boundary."""

from tools.policy import (
    ToolPermissionDecision,
    granted_permission_from_constraints,
    permission_allows,
    permission_rank,
    resolve_bash_command_permission,
)
from tools.registry import (
    DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS,
    DEFAULT_TOOL_REGISTRY,
    EXECUTE_BASH_TOOL,
    EXECUTE_BASH_TOOL_NAME,
    ToolCapabilityCategory,
    ToolDefinition,
    ToolExpectedArtifact,
    ToolPermissionLevel,
    ToolRegistry,
    ToolSideEffectLevel,
    UnknownToolError,
)

__all__ = [
    "DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS",
    "DEFAULT_TOOL_REGISTRY",
    "EXECUTE_BASH_TOOL",
    "EXECUTE_BASH_TOOL_NAME",
    "ToolPermissionDecision",
    "ToolCapabilityCategory",
    "ToolDefinition",
    "ToolExpectedArtifact",
    "ToolPermissionLevel",
    "ToolRegistry",
    "ToolSideEffectLevel",
    "UnknownToolError",
    "granted_permission_from_constraints",
    "permission_allows",
    "permission_rank",
    "resolve_bash_command_permission",
]
