"""MCP-ready tool client layered over the internal registry."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import cached_property
from typing import TypeVar

from pydantic import Field, model_validator

from tools.registry import (
    DEFAULT_TOOL_REGISTRY,
    ToolDefinition,
    ToolModel,
    ToolRegistry,
    UnknownToolError,
)

_ToolEntryT = TypeVar("_ToolEntryT")


class McpToolDescriptor(ToolModel):
    """Normalized MCP-style descriptor for one exposed internal tool."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, object] = Field(default_factory=dict)


def _normalize_registered_tool_name(name: str) -> str:
    """Normalize a stored tool name without collapsing whitespace-only names to empty."""
    stripped_name = name.strip()
    return stripped_name or name


def _normalize_lookup_name(name: str) -> str | None:
    """Normalize an incoming lookup key and reject blank names."""
    normalized_name = name.strip()
    if not normalized_name:
        return None
    return normalized_name


def _build_normalized_tool_map(
    entries: Iterable[_ToolEntryT],
    *,
    get_name: Callable[[_ToolEntryT], str],
) -> dict[str, _ToolEntryT]:
    """Index entries by normalized tool name and reject collisions."""
    normalized_entries: dict[str, _ToolEntryT] = {}
    first_names: dict[str, str] = {}
    duplicate_names: dict[str, set[str]] = {}
    for entry in entries:
        original_name = get_name(entry)
        normalized_name = _normalize_registered_tool_name(original_name)
        if normalized_name in normalized_entries:
            duplicate_names.setdefault(normalized_name, {first_names[normalized_name]}).add(
                original_name
            )
            continue
        normalized_entries[normalized_name] = entry
        first_names[normalized_name] = original_name
    if duplicate_names:
        duplicates = ", ".join(
            f"{normalized_name!r} from {sorted(original_names)!r}"
            for normalized_name, original_names in sorted(duplicate_names.items())
        )
        raise ValueError(
            "MCP-exposed tool names must remain unique after normalization; "
            f"duplicate entries: {duplicates}"
        )
    return normalized_entries


def _descriptor_from_tool_definition(tool: ToolDefinition) -> McpToolDescriptor:
    """Project a concrete registry definition into an MCP-ready descriptor."""
    return McpToolDescriptor(
        name=_normalize_registered_tool_name(tool.name),
        description=tool.description,
        input_schema=tool.mcp_input_schema,
    )


class McpToolClient(ToolModel):
    """Registry-backed tool client ready for future MCP migration."""

    registry: ToolRegistry = Field(default_factory=lambda: DEFAULT_TOOL_REGISTRY)

    @model_validator(mode="after")
    def _validate_mcp_names(self) -> McpToolClient:
        """Ensure the registry contains no tools that collide after MCP normalization."""
        _ = self._tool_definition_map
        return self

    @classmethod
    def from_registry(cls, registry: ToolRegistry) -> McpToolClient:
        """Build a client from an explicit internal tool registry."""
        return cls(registry=registry)

    @cached_property
    def _mcp_tools(self) -> tuple[McpToolDescriptor, ...]:
        """Build MCP-style descriptors from the registry once per client instance."""
        return tuple(self._mcp_tool_map.values())

    @cached_property
    def _mcp_tool_map(self) -> dict[str, McpToolDescriptor]:
        """Index MCP-style descriptors by tool name for repeated lookups."""
        return {
            normalized_name: _descriptor_from_tool_definition(tool)
            for normalized_name, tool in self._tool_definition_map.items()
        }

    @cached_property
    def _tool_definition_map(self) -> dict[str, ToolDefinition]:
        """Index internal definitions by normalized name for repeated lookups."""
        return _build_normalized_tool_map(
            self.registry.list_tools(),
            get_name=lambda tool: tool.name,
        )

    def list_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        """Expose the ordered internal tool definitions behind the client boundary."""
        return self.registry.list_tools()

    def get_tool_definition(self, name: str) -> ToolDefinition | None:
        """Return an internal tool definition when present."""
        normalized_name = _normalize_lookup_name(name)
        if normalized_name is None:
            return None
        return self._tool_definition_map.get(normalized_name)

    def require_tool_definition(self, name: str) -> ToolDefinition:
        """Return an internal tool definition or raise a typed lookup error."""
        tool = self.get_tool_definition(name)
        if tool is None:
            raise UnknownToolError(f"Tool '{name}' is not registered.")
        return tool

    def list_mcp_tools(self) -> tuple[McpToolDescriptor, ...]:
        """Return the ordered MCP-style descriptors exposed by the client."""
        return self._mcp_tools

    def get_mcp_tool(self, name: str) -> McpToolDescriptor | None:
        """Return an MCP-style descriptor when the client exposes it."""
        normalized_name = _normalize_lookup_name(name)
        if normalized_name is None:
            return None
        return self._mcp_tool_map.get(normalized_name)

    def require_mcp_tool(self, name: str) -> McpToolDescriptor:
        """Return an MCP-style descriptor or raise a typed lookup error."""
        tool = self.get_mcp_tool(name)
        if tool is None:
            raise UnknownToolError(f"Tool '{name}' is not registered.")
        return tool


DEFAULT_MCP_TOOL_CLIENT = DEFAULT_TOOL_REGISTRY.mcp_client
