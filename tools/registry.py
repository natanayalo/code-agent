"""Typed tool registry shared by worker prompts and runtime execution."""

from __future__ import annotations

from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from tools.mcp import McpToolClient


class ToolModel(BaseModel):
    """Base model for typed tool configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCapabilityCategory(StrEnum):
    """High-level capability families supported by the worker runtime."""

    SHELL = "shell"


class ToolSideEffectLevel(StrEnum):
    """Coarse side-effect classification for tool usage."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGEROUS_SHELL = "dangerous_shell"


class ToolPermissionLevel(StrEnum):
    """Declared permission classes for runtime policy checks."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    DANGEROUS_SHELL = "dangerous_shell"
    NETWORKED_WRITE = "networked_write"
    GIT_PUSH_OR_DEPLOY = "git_push_or_deploy"


class ToolExpectedArtifact(StrEnum):
    """Artifact categories a tool is expected to produce or update."""

    STDOUT = "stdout"
    STDERR = "stderr"
    CHANGED_FILES = "changed_files"


class ToolDefinition(ToolModel):
    """One explicit worker tool definition."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    capability_category: ToolCapabilityCategory
    side_effect_level: ToolSideEffectLevel
    required_permission: ToolPermissionLevel
    timeout_seconds: int = Field(ge=1)
    network_required: bool = False
    expected_artifacts: tuple[ToolExpectedArtifact, ...] = Field(default_factory=tuple)
    mcp_input_schema: dict[str, Any] = Field(default_factory=dict)
    deterministic: bool = False


class UnknownToolError(LookupError):
    """Raised when a runtime requests a tool that the registry does not expose."""


class ToolRegistry(ToolModel):
    """Immutable registry of tool definitions available to a worker run."""

    tools: tuple[ToolDefinition, ...] = Field(default_factory=tuple)

    @cached_property
    def _tool_map(self) -> dict[str, ToolDefinition]:
        """Build a name-to-definition index for fast repeated tool lookups."""
        return {tool.name: tool for tool in self.tools}

    @cached_property
    def mcp_client(self) -> McpToolClient:
        """Build and cache the MCP-ready client for this immutable registry."""
        from tools.mcp import McpToolClient

        return McpToolClient.from_registry(self)

    @model_validator(mode="after")
    def _validate_unique_names(self) -> ToolRegistry:
        names: set[str] = set()
        duplicates: set[str] = set()
        for tool in self.tools:
            if tool.name in names:
                duplicates.add(tool.name)
            names.add(tool.name)
        if duplicates:
            duplicate_names = ", ".join(sorted(duplicates))
            raise ValueError(f"Tool names must be unique; duplicate entries: {duplicate_names}")
        return self

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        """Return the ordered tool definitions exposed by the registry."""
        return self.tools

    def get_tool(self, name: str) -> ToolDefinition | None:
        """Return a tool definition when the registry contains it."""
        normalized_name = name.strip()
        if not normalized_name:
            return None
        return self._tool_map.get(normalized_name)

    def require_tool(self, name: str) -> ToolDefinition:
        """Return a tool definition or raise a typed lookup error."""
        tool = self.get_tool(name)
        if tool is None:
            raise UnknownToolError(f"Tool '{name}' is not registered.")
        return tool


EXECUTE_BASH_TOOL_NAME = "execute_bash"
DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS = 60

EXECUTE_BASH_TOOL = ToolDefinition(
    name=EXECUTE_BASH_TOOL_NAME,
    description="Run one bash command inside the persistent sandbox workspace.",
    capability_category=ToolCapabilityCategory.SHELL,
    side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
    required_permission=ToolPermissionLevel.WORKSPACE_WRITE,
    timeout_seconds=DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS,
    network_required=False,
    expected_artifacts=(
        ToolExpectedArtifact.STDOUT,
        ToolExpectedArtifact.STDERR,
        ToolExpectedArtifact.CHANGED_FILES,
    ),
    mcp_input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "description": "One bash command to run inside the persistent sandbox workspace.",
            }
        },
        "required": ["command"],
    },
    deterministic=False,
)

DEFAULT_TOOL_REGISTRY = ToolRegistry(tools=(EXECUTE_BASH_TOOL,))
