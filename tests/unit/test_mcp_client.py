"""Unit tests for the MCP-ready internal tool client."""

from __future__ import annotations

import pytest

from tools import (
    DEFAULT_MCP_TOOL_CLIENT,
    DEFAULT_TOOL_REGISTRY,
    McpToolClient,
    ToolExpectedArtifact,
    ToolRegistry,
    UnknownToolError,
)


def test_default_mcp_tool_client_exposes_execute_bash_definition() -> None:
    """The MCP client should expose internal tool definitions through the registry boundary."""
    tool = DEFAULT_MCP_TOOL_CLIENT.require_tool_definition(" execute_bash ")

    assert tool == DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")
    assert tool.expected_artifacts == (
        ToolExpectedArtifact.STDOUT,
        ToolExpectedArtifact.STDERR,
        ToolExpectedArtifact.CHANGED_FILES,
    )


def test_default_mcp_tool_client_exposes_execute_bash_descriptor() -> None:
    """The MCP client should expose a normalized MCP-style descriptor for execute_bash."""
    tool = DEFAULT_MCP_TOOL_CLIENT.require_mcp_tool(" execute_bash ")

    assert tool.name == "execute_bash"
    assert tool.input_schema == {
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
    }


def test_tool_registry_caches_a_reusable_mcp_client() -> None:
    """Explicit registries should reuse one MCP client instance across repeated access."""
    assert DEFAULT_TOOL_REGISTRY.mcp_client is DEFAULT_TOOL_REGISTRY.mcp_client
    assert DEFAULT_TOOL_REGISTRY.mcp_client is DEFAULT_MCP_TOOL_CLIENT


def test_require_mcp_tool_raises_a_typed_error_for_unknown_names() -> None:
    """Unknown MCP tool lookups should preserve the registry's typed error surface."""
    with pytest.raises(UnknownToolError, match="not registered"):
        DEFAULT_MCP_TOOL_CLIENT.require_mcp_tool("missing_tool")


def test_mcp_tool_client_normalizes_descriptor_names_for_lookup() -> None:
    """MCP lookups should still work when the registry contains accidental name spacing."""
    spaced_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash").model_copy(
        update={"name": " execute_bash "}
    )
    tool_client = McpToolClient.from_registry(ToolRegistry(tools=(spaced_tool,)))

    tool = tool_client.require_mcp_tool("execute_bash")

    assert tool.name == "execute_bash"


def test_mcp_tool_client_normalizes_internal_tool_definition_lookup() -> None:
    """Internal definition lookups should normalize names through the MCP client boundary."""
    spaced_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash").model_copy(
        update={"name": " execute_bash "}
    )
    tool_client = McpToolClient.from_registry(ToolRegistry(tools=(spaced_tool,)))

    tool = tool_client.require_tool_definition("execute_bash")

    assert tool.name == " execute_bash "


def test_mcp_tool_client_preserves_whitespace_only_registered_names_in_descriptors() -> None:
    """Whitespace-only registry names should not crash descriptor creation."""
    whitespace_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash").model_copy(
        update={"name": "   "}
    )
    tool_client = McpToolClient.from_registry(ToolRegistry(tools=(whitespace_tool,)))

    tool = tool_client.list_mcp_tools()[0]

    assert tool.name == "   "
    assert tool_client.get_mcp_tool("   ") is None


def test_mcp_tool_client_rejects_duplicate_normalized_names() -> None:
    """Normalized MCP names should remain unique across the client boundary."""
    execute_bash_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")
    spaced_tool = execute_bash_tool.model_copy(update={"name": " execute_bash "})
    tool_client = McpToolClient.from_registry(ToolRegistry(tools=(execute_bash_tool, spaced_tool)))

    with pytest.raises(ValueError, match="unique after normalization"):
        tool_client.require_mcp_tool("execute_bash")
