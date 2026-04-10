"""MCP-ready tool client layered over the internal registry."""

from __future__ import annotations

from functools import cached_property

from pydantic import Field

from tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    ToolDefinition,
    ToolModel,
    ToolRegistry,
    UnknownToolError,
)


class McpToolDescriptor(ToolModel):
    """Normalized MCP-style descriptor for one exposed internal tool."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, object] = Field(default_factory=dict)


def _descriptor_from_tool_definition(tool: ToolDefinition) -> McpToolDescriptor:
    """Project a concrete registry definition into an MCP-ready descriptor."""
    return McpToolDescriptor(
        name=tool.name.strip(),
        description=tool.description,
        input_schema=tool.mcp_input_schema,
    )


class McpToolClient(ToolModel):
    """Registry-backed tool client ready for future MCP migration."""

    registry: ToolRegistry = Field(default_factory=lambda: DEFAULT_TOOL_REGISTRY)

    @classmethod
    def from_registry(cls, registry: ToolRegistry) -> McpToolClient:
        """Build a client from an explicit internal tool registry."""
        return cls(registry=registry)

    @cached_property
    def _mcp_tools(self) -> tuple[McpToolDescriptor, ...]:
        """Build MCP-style descriptors from the registry once per client instance."""
        return tuple(_descriptor_from_tool_definition(tool) for tool in self.registry.list_tools())

    @cached_property
    def _mcp_tool_map(self) -> dict[str, McpToolDescriptor]:
        """Index MCP-style descriptors by tool name for repeated lookups."""
        return {tool.name: tool for tool in self._mcp_tools}

    def list_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """Expose the ordered internal tool definitions behind the client boundary."""
        return self.registry.list_tools()

    def get_tool_definition(self, name: str) -> ToolDefinition | None:
        """Return an internal tool definition when present."""
        return self.registry.get_tool(name)

    def require_tool_definition(self, name: str) -> ToolDefinition:
        """Return an internal tool definition or raise a typed lookup error."""
        return self.registry.require_tool(name)

    def list_mcp_tools(self) -> tuple[McpToolDescriptor, ...]:
        """Return the ordered MCP-style descriptors exposed by the client."""
        return self._mcp_tools

    def get_mcp_tool(self, name: str) -> McpToolDescriptor | None:
        """Return an MCP-style descriptor when the client exposes it."""
        normalized_name = name.strip()
        if not normalized_name:
            return None
        return self._mcp_tool_map.get(normalized_name)

    def require_mcp_tool(self, name: str) -> McpToolDescriptor:
        """Return an MCP-style descriptor or raise a typed lookup error."""
        tool = self.get_mcp_tool(name)
        if tool is None:
            raise UnknownToolError(f"Tool '{name}' is not registered.")
        return tool


DEFAULT_MCP_TOOL_CLIENT = DEFAULT_TOOL_REGISTRY.mcp_client
