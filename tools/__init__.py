"""Tool integration package boundary."""

from tools.mcp import DEFAULT_MCP_TOOL_CLIENT, McpToolClient, McpToolDescriptor
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
    "DEFAULT_MCP_TOOL_CLIENT",
    "DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS",
    "DEFAULT_TOOL_REGISTRY",
    "EXECUTE_BASH_TOOL",
    "EXECUTE_BASH_TOOL_NAME",
    "McpToolClient",
    "McpToolDescriptor",
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
