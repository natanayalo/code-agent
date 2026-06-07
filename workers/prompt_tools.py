import json
import re

from tools import DEFAULT_MCP_TOOL_CLIENT, McpToolClient, ToolDefinition, ToolRegistry


def build_available_tools_section(
    tool_registry: ToolRegistry | None = None,
    tool_client: McpToolClient | None = None,
    allowed_tool_names: set[str] | None = None,
) -> str:
    """Render the configured worker tool surface."""
    resolved_client = tool_client or (
        DEFAULT_MCP_TOOL_CLIENT if tool_registry is None else tool_registry.mcp_client
    )
    tools = resolved_client.list_tool_definitions()
    if allowed_tool_names is not None:
        tools = tuple(tool for tool in tools if tool.name in allowed_tool_names)

    if not tools:
        return "## Available Tools\n- No tools configured."
    tool_sections = [_render_tool_definition(tool) for tool in tools]
    return "\n".join(["## Available Tools", *tool_sections])


def _extract_available_tool_names_from_system_prompt(system_prompt: str | None) -> set[str] | None:
    """Return tool names listed in the prompt's Available Tools section."""
    if system_prompt is None:
        return None
    stripped_prompt = system_prompt.strip()
    if not stripped_prompt:
        return None
    match = re.search(r"(?ms)^## Available Tools\s*(.+?)(?:\n## |\Z)", stripped_prompt)
    if match is None:
        return None
    section_body = match.group(1)
    names = {
        name.strip()
        for name in re.findall(r"^### `([^`]+)`\s*$", section_body, flags=re.MULTILINE)
        if name.strip()
    }
    return names


def _schema_type_names(raw_type: object) -> tuple[str, ...]:
    """Normalize JSON-schema type declarations into a deterministic tuple."""
    if isinstance(raw_type, str):
        return (raw_type,)
    if isinstance(raw_type, list):
        normalized = [item for item in raw_type if isinstance(item, str)]
        return tuple(normalized)
    return ()


def _looks_like_single_command_schema(tool: ToolDefinition) -> bool:
    """Return whether a tool schema expects one plain command string."""
    schema = tool.mcp_input_schema
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return False
    if tuple(required) != ("command",) or set(properties) != {"command"}:
        return False
    command_property = properties.get("command")
    if not isinstance(command_property, dict):
        return False
    return "string" in _schema_type_names(command_property.get("type"))


def _example_value_from_schema(property_name: str, property_schema: object) -> object:
    """Build one compact example value from a JSON-schema property."""
    if not isinstance(property_schema, dict):
        return f"<{property_name}>"

    enum_values = property_schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        return enum_values[0]

    type_names = set(_schema_type_names(property_schema.get("type")))
    if "boolean" in type_names:
        return True
    if "integer" in type_names:
        return 1
    if "number" in type_names:
        return 1
    if "array" in type_names:
        item_schema = property_schema.get("items")
        return [_example_value_from_schema(f"{property_name}_item", item_schema)]
    if "object" in type_names:
        properties = property_schema.get("properties")
        required = property_schema.get("required")
        if isinstance(properties, dict) and isinstance(required, list):
            payload: dict[str, object] = {}
            for nested_name in required:
                if not isinstance(nested_name, str) or not nested_name:
                    continue
                payload[nested_name] = _example_value_from_schema(
                    nested_name,
                    properties.get(nested_name),
                )
            return payload
        return {}
    if type_names == {"null"}:
        return None
    return f"<{property_name}>"


def _build_tool_input_example(tool: ToolDefinition) -> str | None:
    """Build a compact tool-input example payload from required schema fields."""
    schema = tool.mcp_input_schema
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return None

    payload: dict[str, object] = {}
    for property_name in required:
        if not isinstance(property_name, str) or not property_name:
            continue
        payload[property_name] = _example_value_from_schema(
            property_name,
            properties.get(property_name),
        )
    if not payload:
        return None
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _render_tool_input_guidance(tool: ToolDefinition) -> str:
    """Render one deterministic adapter hint for tool_input shape."""
    if _looks_like_single_command_schema(tool):
        return f"- For `{tool.name}`, return one focused shell command as the `tool_input` string."

    schema = tool.mcp_input_schema
    required_keys: tuple[str, ...] = ()
    operation_variants = ""
    if isinstance(schema, dict):
        raw_required = schema.get("required")
        if isinstance(raw_required, list):
            required_keys = tuple(
                key for key in raw_required if isinstance(key, str) and key.strip()
            )
        properties = schema.get("properties")
        if isinstance(properties, dict):
            operation_property = properties.get("operation")
            if isinstance(operation_property, dict):
                operation_enum = operation_property.get("enum")
                if isinstance(operation_enum, list) and operation_enum:
                    normalized_ops = [
                        value
                        for value in operation_enum
                        if isinstance(value, str) and value.strip()
                    ]
                    if normalized_ops:
                        shown_ops = normalized_ops[:4]
                        operations = ", ".join(f"`{value}`" for value in shown_ops)
                        if len(normalized_ops) > len(shown_ops):
                            operations = f"{operations}, ..."
                        operation_variants = f"; supported operations: {operations}"

    required_fragment = ""
    if required_keys:
        rendered_required = ", ".join(f"`{key}`" for key in required_keys)
        noun = "key" if len(required_keys) == 1 else "keys"
        required_fragment = f" (required {noun}: {rendered_required})"

    example = _build_tool_input_example(tool)
    example_fragment = f", for example {example}" if example is not None else ""
    return (
        f"- For `{tool.name}`, encode `tool_input` as a compact JSON object string"
        f"{required_fragment}{operation_variants}{example_fragment}."
    )


def build_runtime_adapter_tool_guidance_lines(
    *,
    tool_registry: ToolRegistry | None = None,
    tool_client: McpToolClient | None = None,
    system_prompt: str | None = None,
) -> list[str]:
    """Render shared tool-input guidance lines for runtime adapters."""
    resolved_client = tool_client or (
        DEFAULT_MCP_TOOL_CLIENT if tool_registry is None else tool_registry.mcp_client
    )
    tools = list(resolved_client.list_tool_definitions())
    supported_names = _extract_available_tool_names_from_system_prompt(system_prompt)
    if supported_names is not None:
        tools = [tool for tool in tools if tool.name in supported_names]
    return [_render_tool_input_guidance(tool) for tool in tools]


def _render_tool_definition(tool: ToolDefinition) -> str:
    """Render one tool definition for prompt injection."""
    expected_artifacts = (
        ", ".join(f"`{artifact.value}`" for artifact in tool.expected_artifacts)
        if tool.expected_artifacts
        else None
    )
    lines = [f"### `{tool.name}`", tool.description]
    if tool.required_permission.value != "none":
        lines.append(f"Required permission: `{tool.required_permission.value}`")
    if expected_artifacts:
        lines.append(f"Expected artifacts: {expected_artifacts}")
    return "\n".join(lines)
