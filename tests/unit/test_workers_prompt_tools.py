"""Unit tests for workers/prompt_tools.py."""

from __future__ import annotations

from unittest.mock import MagicMock

from tools import (
    ToolCapabilityCategory,
    ToolDefinition,
    ToolPermissionLevel,
    ToolSideEffectLevel,
)
from workers.prompt_tools import (
    _example_value_from_schema,
    _extract_available_tool_names_from_system_prompt,
    _render_tool_definition,
    build_available_tools_section,
    build_runtime_adapter_tool_guidance_lines,
)


def test_build_available_tools_section():
    client = MagicMock()

    t1 = ToolDefinition(
        name="run_command",
        description="Run a shell command",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
        required_permission=ToolPermissionLevel.READ_ONLY,
        timeout_seconds=30,
        mcp_input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    )
    t2 = ToolDefinition(
        name="custom_tool",
        description="Custom tool",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
        required_permission=ToolPermissionLevel.DANGEROUS_SHELL,
        timeout_seconds=60,
        mcp_input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "write", "delete", "sync", "extra"],
                },
                "flag": {"type": "boolean"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "meta": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
                "optional_null": {"type": "null"},
            },
            "required": ["operation", "flag", "count", "ratio", "tags", "meta", "optional_null"],
        },
    )

    client.list_tool_definitions.return_value = (t1, t2)

    # Empty filter returns no tools
    res_empty = build_available_tools_section(tool_client=client, allowed_tool_names=set())
    assert res_empty == "## Available Tools\n- No tools configured."

    # Filtered
    res_filtered = build_available_tools_section(
        tool_client=client, allowed_tool_names={"run_command"}
    )
    assert "### `run_command`" in res_filtered
    assert "### `custom_tool`" not in res_filtered

    # Unfiltered
    res_all = build_available_tools_section(tool_client=client)
    assert "### `run_command`" in res_all
    assert "### `custom_tool`" in res_all

    assert "Required permission: `dangerous_shell`" in res_all

    # _render_tool_definition with expected artifacts
    t3 = ToolDefinition(
        name="tool_with_artifacts",
        description="Tool with artifacts",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
        required_permission=ToolPermissionLevel.WORKSPACE_WRITE,
        timeout_seconds=30,
        expected_artifacts=("stdout", "changed_files"),
    )
    rendered = _render_tool_definition(t3)
    assert "Expected artifacts: `stdout`, `changed_files`" in rendered
    assert "Required permission: `workspace_write`" in rendered


def test_extract_available_tool_names_from_system_prompt():
    assert _extract_available_tool_names_from_system_prompt(None) is None
    assert _extract_available_tool_names_from_system_prompt("  ") is None
    assert _extract_available_tool_names_from_system_prompt("No tools section here") is None

    prompt = """
## Header
Some info

## Available Tools
### `run_command`
Run command

### `view_file`
View file

## Other section
"""
    names = _extract_available_tool_names_from_system_prompt(prompt)
    assert names == {"run_command", "view_file"}


def test_example_value_from_schema_variants():
    assert _example_value_from_schema("prop", "not_a_dict") == "<prop>"
    assert _example_value_from_schema("enum_prop", {"enum": ["val1", "val2"]}) == "val1"
    assert _example_value_from_schema("bool_prop", {"type": "boolean"}) is True
    assert _example_value_from_schema("int_prop", {"type": "integer"}) == 1
    assert _example_value_from_schema("num_prop", {"type": "number"}) == 1
    assert _example_value_from_schema(
        "arr_prop", {"type": "array", "items": {"type": "string"}}
    ) == ["<arr_prop_item>"]
    assert _example_value_from_schema(
        "obj_prop", {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    ) == {"a": "<a>"}
    assert _example_value_from_schema("obj_prop_empty", {"type": "object"}) == {}
    assert _example_value_from_schema("null_prop", {"type": "null"}) is None


def test_render_tool_input_guidance_and_lines():
    t1 = ToolDefinition(
        name="run_command",
        description="Run a command",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
        required_permission=ToolPermissionLevel.READ_ONLY,
        timeout_seconds=30,
        mcp_input_schema={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    )
    t2 = ToolDefinition(
        name="complex_tool",
        description="Complex tool",
        capability_category=ToolCapabilityCategory.SHELL,
        side_effect_level=ToolSideEffectLevel.WORKSPACE_WRITE,
        required_permission=ToolPermissionLevel.READ_ONLY,
        timeout_seconds=60,
        mcp_input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["op1", "op2", "op3", "op4", "op5"]},
                "target": {"type": "string"},
            },
            "required": ["operation", "target"],
        },
    )

    client = MagicMock()
    client.list_tool_definitions.return_value = (t1, t2)

    prompt = "## Available Tools\n### `complex_tool`\n"
    lines = build_runtime_adapter_tool_guidance_lines(tool_client=client, system_prompt=prompt)
    assert len(lines) == 1
    assert "complex_tool" in lines[0]
