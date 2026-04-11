"""Unit tests for the explicit worker tool registry."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tools import (
    DEFAULT_TOOL_REGISTRY,
    ToolCapabilityCategory,
    ToolExpectedArtifact,
    ToolPermissionLevel,
    ToolRegistry,
    ToolSideEffectLevel,
    UnknownToolError,
)


def test_default_tool_registry_exposes_execute_bash_metadata() -> None:
    """The default registry should expose the canonical bash tool definition."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool(" execute_bash ")

    assert tool.capability_category == ToolCapabilityCategory.SHELL
    assert tool.side_effect_level == ToolSideEffectLevel.WORKSPACE_WRITE
    assert tool.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert tool.timeout_seconds == 60
    assert tool.expected_artifacts == (
        ToolExpectedArtifact.STDOUT,
        ToolExpectedArtifact.STDERR,
        ToolExpectedArtifact.CHANGED_FILES,
    )


def test_default_tool_registry_exposes_execute_git_metadata() -> None:
    """The default registry should expose the canonical git helper definition."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool(" execute_git ")

    assert tool.capability_category == ToolCapabilityCategory.GIT
    assert tool.side_effect_level == ToolSideEffectLevel.WORKSPACE_WRITE
    assert tool.required_permission == ToolPermissionLevel.WORKSPACE_WRITE
    assert tool.timeout_seconds == 30
    assert tool.expected_artifacts == (
        ToolExpectedArtifact.STDOUT,
        ToolExpectedArtifact.STDERR,
        ToolExpectedArtifact.CHANGED_FILES,
    )


def test_default_tool_registry_exposes_execute_github_metadata() -> None:
    """The default registry should expose the canonical GitHub helper definition."""
    tool = DEFAULT_TOOL_REGISTRY.require_tool(" execute_github ")

    assert tool.capability_category == ToolCapabilityCategory.GITHUB
    assert tool.side_effect_level == ToolSideEffectLevel.WORKSPACE_WRITE
    assert tool.required_permission == ToolPermissionLevel.NETWORKED_WRITE
    assert tool.timeout_seconds == 60
    assert tool.network_required is True
    assert tool.expected_artifacts == (
        ToolExpectedArtifact.STDOUT,
        ToolExpectedArtifact.STDERR,
    )


def test_tool_registry_rejects_duplicate_tool_names() -> None:
    """Duplicate tool names should fail validation at the registry boundary."""
    execute_bash_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash")

    with pytest.raises(ValidationError, match="duplicate entries: execute_bash"):
        ToolRegistry(tools=(execute_bash_tool, execute_bash_tool))


def test_tool_registry_rejects_whitespace_only_tool_names() -> None:
    """Whitespace-only tool names should fail validation at the registry boundary."""
    whitespace_tool = DEFAULT_TOOL_REGISTRY.require_tool("execute_bash").model_copy(
        update={"name": "   "}
    )

    with pytest.raises(ValidationError, match="non-whitespace character"):
        ToolRegistry(tools=(whitespace_tool,))


def test_require_tool_raises_a_typed_error_for_unknown_names() -> None:
    """Missing tools should surface a dedicated lookup error."""
    with pytest.raises(UnknownToolError, match="not registered"):
        DEFAULT_TOOL_REGISTRY.require_tool("missing_tool")


def test_get_tool_returns_registered_definitions_for_trimmed_names() -> None:
    """Direct lookups should still normalize names before using the cached map."""
    tool = DEFAULT_TOOL_REGISTRY.get_tool(" execute_bash ")

    assert tool is not None
    assert tool.name == "execute_bash"
