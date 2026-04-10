"""Tool integration package boundary."""

from tools.git import GitOperation, GitToolError, GitToolRequest, build_git_command_from_input
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
    DEFAULT_EXECUTE_GIT_TIMEOUT_SECONDS,
    DEFAULT_TOOL_REGISTRY,
    EXECUTE_BASH_TOOL,
    EXECUTE_BASH_TOOL_NAME,
    EXECUTE_GIT_TOOL,
    EXECUTE_GIT_TOOL_NAME,
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
    "DEFAULT_EXECUTE_GIT_TIMEOUT_SECONDS",
    "DEFAULT_TOOL_REGISTRY",
    "EXECUTE_BASH_TOOL",
    "EXECUTE_BASH_TOOL_NAME",
    "EXECUTE_GIT_TOOL",
    "EXECUTE_GIT_TOOL_NAME",
    "GitOperation",
    "GitToolError",
    "GitToolRequest",
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
    "build_git_command_from_input",
    "permission_allows",
    "permission_rank",
    "resolve_bash_command_permission",
]
