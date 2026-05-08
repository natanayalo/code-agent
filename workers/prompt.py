"""Structured system prompt construction helpers for coding workers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools import DEFAULT_MCP_TOOL_CLIENT, McpToolClient, ToolDefinition, ToolRegistry
from workers.base import WorkerRequest
from workers.markdown import markdown_fence_for_content

DEFAULT_REPO_LISTING_MAX_DEPTH = 2
DEFAULT_REPO_LISTING_MAX_ENTRIES = 40
DEFAULT_AGENTS_MAX_CHARACTERS = 5000
DEFAULT_AGENTS_ASSET_READ_MAX_CHARACTERS = 8192
DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS = 3000
_SECTION_SEPARATOR_OVERHEAD_BUFFER = 32
_GUIDANCE_OVERHEAD_BUFFER = 100
_REVIEW_ROLE_SECTION = "\n".join(
    [
        "## Review Role",
        "You are the review worker for code-agent.",
        "Focus on high-confidence, actionable findings grounded in the supplied context.",
        "Prefer precision over recall and skip style-only or speculative comments.",
        "Do not propose broad rewrites when a focused finding is sufficient.",
    ]
)
_REVIEW_SCHEMA_PAYLOAD = {
    "reviewer_kind": "string",
    "summary": "string",
    "confidence": 0.0,
    "outcome": "no_findings|findings",
    "findings": [
        {
            "severity": "low|medium|high|critical",
            "category": "string",
            "confidence": 0.0,
            "file_path": "string",
            "line_start": 1,
            "line_end": 1,
            "title": "string",
            "why_it_matters": "string",
            "evidence": "string|null",
            "suggested_fix": "string|null",
        }
    ],
}
_REVIEW_OUTPUT_CONTRACT_TEMPLATE = "\n".join(
    [
        "## Output Contract",
        "Return exactly one JSON object. Your response MUST NOT contain any markdown ",
        "fences or extra prose outside of the JSON payload.",
        "Schema:",
        "```json",
        "{schema_json}",
        "```",
        "Rules:",
        "- Use outcome `no_findings` with an empty `findings` list when nothing actionable exists.",
        "- Use outcome `findings` only when at least one concrete actionable finding exists.",
        "- Keep findings bounded to the supplied review context packet.",
    ]
)
_TRUNCATED_MARKER = "\n... (truncated)"
_AGENTS_ASSET_DIRECTORIES = ("skills", "workflows", "rules")
_BUILD_CONTEXT_ITEM_LIMIT = 8
_BUILD_CONTEXT_VALUE_MAX_CHARACTERS = 220
_SKIPPED_PATH_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}


def _fenced_text_block_lines(label: str, content: str, *, fence: str | None = None) -> list[str]:
    """Render a text block with a collision-safe markdown fence."""
    actual_fence = fence if fence is not None else markdown_fence_for_content(content)
    return [label, f"{actual_fence}text", content, actual_fence]


def _fenced_text_block_overhead(label: str, content: str, *, fence: str | None = None) -> int:
    """Return wrapper-character overhead for one fenced guidance block."""
    actual_fence = fence if fence is not None else markdown_fence_for_content(content)
    # label + opening fence + closing fence + three line separators
    return len(label) + len(f"{actual_fence}text") + len(actual_fence) + 3


def build_role_description_section(request: WorkerRequest) -> str:
    """Describe the worker's job and guardrails."""
    is_read_only = bool(request.constraints.get("read_only"))
    permissions = "read" if is_read_only else "read/write"
    action = "analyze files" if is_read_only else "make smallest safe changes"
    mutation_guard = " Do not modify files." if is_read_only else ""

    return "\n".join(
        [
            "## Role",
            f"You are the coding execution worker with {permissions} permissions. "
            f"Work inside the repository, {action}.{mutation_guard}"
            "Keep reasoning grounded in observed state. Do not bypass sandbox rules.",
        ]
    )


def build_available_tools_section(
    tool_registry: ToolRegistry | None = None,
    tool_client: McpToolClient | None = None,
) -> str:
    """Render the configured worker tool surface."""
    resolved_client = tool_client or (
        DEFAULT_MCP_TOOL_CLIENT if tool_registry is None else tool_registry.mcp_client
    )
    tools = resolved_client.list_tool_definitions()
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


def read_workspace_agents_guidance(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_AGENTS_MAX_CHARACTERS,
) -> str | None:
    """Return bounded AGENTS.md guidance from the workspace root when present."""
    agents_path = workspace_path / "AGENTS.md"
    if not agents_path.is_file():
        return None

    contents = agents_path.read_text(encoding="utf-8", errors="replace").strip()
    if len(contents) <= max_characters:
        return contents
    truncated = contents[:max_characters].rstrip()
    return f"{truncated}\n... (truncated)"


def _truncate_to_budget(value: str, *, max_characters: int) -> str:
    """Truncate text while keeping the result length bounded."""
    if max_characters <= 0:
        return ""
    if len(value) <= max_characters:
        return value
    marker = _TRUNCATED_MARKER
    if len(marker) >= max_characters:
        return marker[:max_characters]
    available = max_characters - len(marker)
    return f"{value[:available].rstrip()}{marker}"


def _read_text_prefix(path: Path, *, max_characters: int) -> str:
    """Read up to a bounded number of characters from a text file."""
    if max_characters <= 0:
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as file_handle:
        return file_handle.read(max_characters)


def _extract_front_matter_metadata(contents: str) -> tuple[str | None, str | None, str]:
    """Extract markdown front matter name/description and return remaining body."""
    lines = contents.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None, contents

    closing_index = None
    for index in range(1, min(len(lines), 50)):
        if lines[index].strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        return None, None, contents

    name: str | None = None
    description: str | None = None
    for line in lines[1:closing_index]:
        key, separator, raw_value = line.partition(":")
        if separator != ":":
            continue
        normalized_key = key.strip().lower()
        value = raw_value.strip()
        if not value:
            continue
        if normalized_key == "name":
            name = value
        elif normalized_key == "description":
            description = value

    body = "\n".join(lines[closing_index + 1 :]).strip()
    return name, description, body


def _first_meaningful_line(contents: str) -> str | None:
    """Return the first non-empty content line useful for a compact summary."""
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            stripped = line.lstrip("#").strip()
            if stripped:
                return stripped
            continue
        return line
    return None


def _summarize_agents_asset(
    file_path: Path,
    *,
    category: str,
    relative_path: str,
) -> str | None:
    """Render one .agents markdown file into a concise prompt summary line."""
    try:
        contents = _read_text_prefix(
            file_path,
            max_characters=DEFAULT_AGENTS_ASSET_READ_MAX_CHARACTERS,
        ).strip()
    except OSError:
        return None
    if not contents:
        return None

    name, description, body = _extract_front_matter_metadata(contents)
    summary_name = name or file_path.stem
    summary_text = description or _first_meaningful_line(body)

    if summary_text:
        return f"- {category}/{relative_path}: {summary_name} - {summary_text}"
    return f"- {category}/{relative_path}: {summary_name}"


def read_workspace_agents_assets_guidance(
    workspace_path: Path,
    *,
    max_characters: int,
) -> str | None:
    """Return bounded summaries of markdown assets under .agents/."""
    if max_characters <= 0:
        return None

    lines: list[str] = []
    current_characters = 0
    exceeded_budget = False
    agents_root = workspace_path / ".agents"
    if not agents_root.is_dir():
        return None

    for category in _AGENTS_ASSET_DIRECTORIES:
        category_path = agents_root / category
        if not category_path.is_dir():
            continue
        for file_path in sorted(
            category_path.rglob("*.md"),
            key=lambda path: path.as_posix().lower(),
        ):
            if not file_path.is_file():
                continue
            relative_path = file_path.relative_to(category_path).as_posix()
            summary_line = _summarize_agents_asset(
                file_path,
                category=category,
                relative_path=relative_path,
            )
            if summary_line is None:
                continue
            lines.append(summary_line)
            if current_characters:
                current_characters += 1
            current_characters += len(summary_line)
            if current_characters > max_characters:
                exceeded_budget = True
                break
        if exceeded_budget:
            break

    if not lines:
        return None
    return _truncate_to_budget("\n".join(lines), max_characters=max_characters)


def read_workspace_repo_guidance(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_AGENTS_MAX_CHARACTERS,
) -> tuple[str | None, str | None]:
    """Return bounded AGENTS.md and .agents guidance within one shared budget."""
    if max_characters <= 0:
        return None, None

    agents_path = workspace_path / "AGENTS.md"
    agents_guidance: str | None = None
    remaining = max_characters

    if agents_path.is_file():
        try:
            agents_contents = _read_text_prefix(
                agents_path,
                max_characters=remaining + 1,
            ).strip()
        except OSError:
            agents_contents = ""
        if agents_contents:
            agents_guidance = _truncate_to_budget(agents_contents, max_characters=remaining)
            remaining -= len(agents_guidance)

    agents_assets_guidance = read_workspace_agents_assets_guidance(
        workspace_path,
        max_characters=max(remaining, 0),
    )
    return agents_guidance, agents_assets_guidance


def build_workspace_directory_listing(
    workspace_path: Path,
    *,
    max_depth: int = DEFAULT_REPO_LISTING_MAX_DEPTH,
    max_entries: int = DEFAULT_REPO_LISTING_MAX_ENTRIES,
) -> str:
    """Build a deterministic bounded directory listing for prompt context."""
    root = workspace_path
    if not root.exists():
        return "<workspace path does not exist>"
    if not root.is_dir():
        return "<workspace path is not a directory>"

    entries: list[str] = []
    truncated = False

    def visit(path: Path, *, depth: int) -> None:
        nonlocal truncated
        if truncated or depth >= max_depth:
            return

        try:
            children = sorted(
                (child for child in path.iterdir() if child.name not in _SKIPPED_PATH_NAMES),
                key=lambda child: (not child.is_dir(), child.name.lower()),
            )
        except OSError:
            return
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                return
            relative_path = child.relative_to(root).as_posix()
            entries.append(f"{relative_path}/" if child.is_dir() else relative_path)
            if child.is_dir():
                visit(child, depth=depth + 1)

    visit(root, depth=0)

    if not entries:
        return "<workspace is empty>"
    if truncated:
        entries.append("... (truncated)")
    return "\n".join(entries)


def build_repo_context_section(workspace_path: Path) -> str:
    """Render repo-level prompt context from the workspace."""
    return _build_repo_context_section_with_guidance(
        workspace_path,
        *read_workspace_repo_guidance(workspace_path),
    )


def _build_repo_context_section_with_guidance(
    workspace_path: Path,
    agents_guidance: str | None,
    agents_assets_guidance: str | None,
) -> str:
    """Render repo-level prompt context from pre-resolved guidance snippets."""
    lines = [
        "## Repo Context",
        "Directory listing:",
        "```text",
        build_workspace_directory_listing(workspace_path),
        "```",
    ]
    if agents_guidance is not None:
        lines.extend(_fenced_text_block_lines("AGENTS.md guidance:", agents_guidance))
    if agents_assets_guidance is not None:
        lines.extend(_fenced_text_block_lines(".agents guidance:", agents_assets_guidance))
    return "\n".join(lines)


def _compact_json_summary(value: Any) -> str:
    """Render JSON data into one bounded line for prompt summaries."""
    serialized = json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))
    return _truncate_to_budget(serialized, max_characters=_BUILD_CONTEXT_VALUE_MAX_CHARACTERS)


def _extract_makefile_targets(contents: str) -> list[str]:
    """Extract a bounded set of non-special Makefile targets."""
    targets: list[str] = []
    for line in contents.splitlines():
        match = re.match(
            r"^([A-Za-z0-9][A-Za-z0-9_.%/-]*(?:\s+[A-Za-z0-9][A-Za-z0-9_.%/-]*)*)\s*:(?!\s*=)",
            line,
        )
        if match is None:
            continue
        for target in match.group(1).split():
            if target.startswith(".") or "%" in target:
                continue
            if target not in targets:
                targets.append(target)
            if len(targets) >= _BUILD_CONTEXT_ITEM_LIMIT:
                return targets
    return targets


def _render_build_test_info(path: Path) -> str | None:
    """Render a compact summary of build/test metadata."""
    parts = []
    if (path / "pyproject.toml").exists():
        parts.append("- Build/Test config found in `pyproject.toml`.")
    if (path / "Dockerfile").exists():
        parts.append("- Deployment config found in `Dockerfile`.")
    if (path / ".github/workflows").is_dir():
        parts.append("- CI workflows found in `.github/workflows/`.")
    if not parts:
        return None
    return "\n".join(["## Build & Test", *parts])


def _json_safe(value: Any) -> Any:
    """Normalize prompt context data into JSON-safe values."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(
            (_json_safe(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    return str(value)


def _render_json_block(value: Any) -> str:
    """Pretty-print a JSON-safe object for prompt inclusion."""
    return json.dumps(_json_safe(value), indent=2, sort_keys=True)


def _mask_repo_url(repo_url: str | None) -> str | None:
    """Hide inline credentials before rendering repository context in prompts."""
    if repo_url is None:
        return None
    scheme, marker, remainder = repo_url.partition("://")
    if not marker or "@" not in remainder:
        return repo_url
    credentials, _, host = remainder.rpartition("@")
    if not credentials:
        return repo_url
    return f"{scheme}{marker}***@{host}"


def build_task_context_section(request: WorkerRequest) -> str:
    """Render task-specific prompt context from the normalized worker request."""
    lines = [
        "## Task Context",
        f"Goal: {request.task_text}",
    ]

    spec = request.task_spec or {}
    if spec:
        for field in ("assumptions", "acceptance_criteria", "non_goals", "verification_commands"):
            values = spec.get(field)
            if isinstance(values, list) and values:
                lines.append(f"{field.replace('_', ' ').capitalize()}:")
                lines.extend([f"- {v}" for v in values])

    if request.constraints:
        # Only include high-level constraints, omit implementation details like lint commands
        visible_constraints = {
            k: v
            for k, v in request.constraints.items()
            if k in {"risk_level", "priority", "read_only", "requires_approval"}
        }
        if visible_constraints:
            lines.extend(
                ["Constraints:", "```json", _render_json_block(visible_constraints), "```"]
            )

    return "\n".join(lines)


def build_workflow_instructions_section(request: WorkerRequest) -> str:
    """Describe the expected worker execution workflow."""
    is_read_only = bool(request.constraints.get("read_only"))
    lines = [
        "## Workflow",
        "- Inspect files before making decisions.",
    ]
    if not is_read_only:
        lines.append("- Prefer minimal edits over broad rewrites.")

    lines.extend(
        [
            "- Use tools with focused commands; avoid dumping large files.",
            "- Base steps on observed output and exit codes.",
            "- Surface blockers explicitly instead of guessing.",
            "- End with a concise summary.",
        ]
    )
    return "\n".join(lines)


def read_workspace_review_guidance(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS,
) -> str | None:
    """Return bounded REVIEW.md guidance from the workspace root when present."""
    review_path = workspace_path / "REVIEW.md"
    if not review_path.is_file() or max_characters <= 0:
        return None
    try:
        contents = _read_text_prefix(review_path, max_characters=max_characters + 1).strip()
    except OSError:
        return None
    if not contents:
        return None
    return _truncate_to_budget(contents, max_characters=max_characters)


def build_review_prompt(
    *,
    workspace_path: Path,
    review_context_packet: str,
    reviewer_kind: str = "worker_self_review",
    task_text: str | None = None,
) -> str:
    """Assemble a review-only prompt separated from execution/tool-loop prompts."""
    # Reserve a small budget buffer for \n\n separators between prompt sections
    # and a buffer for block labels/fences added after reading guidance.
    total_guidance_budget = (
        DEFAULT_REVIEW_GUIDANCE_MAX_CHARACTERS
        - _SECTION_SEPARATOR_OVERHEAD_BUFFER
        - _GUIDANCE_OVERHEAD_BUFFER
    )
    agents_guidance, agents_assets_guidance = read_workspace_repo_guidance(
        workspace_path,
        max_characters=total_guidance_budget,
    )

    guidance_lines: list[str] = []
    consumed_guidance_characters = 0
    guidance_block_count = 0

    if agents_guidance is not None:
        if guidance_block_count == 0:
            consumed_guidance_characters += len("## Review Guidance") + 1
        else:
            consumed_guidance_characters += 1  # separator between blocks

        fence = markdown_fence_for_content(agents_guidance)
        guidance_lines.extend(
            _fenced_text_block_lines("AGENTS.md guidance:", agents_guidance, fence=fence)
        )
        consumed_guidance_characters += len(agents_guidance) + _fenced_text_block_overhead(
            "AGENTS.md guidance:", agents_guidance, fence=fence
        )
        guidance_block_count += 1

    if agents_assets_guidance is not None:
        if guidance_block_count == 0:
            consumed_guidance_characters += len("## Review Guidance") + 1
        else:
            consumed_guidance_characters += 1  # separator between blocks

        fence = markdown_fence_for_content(agents_assets_guidance)
        guidance_lines.extend(
            _fenced_text_block_lines(".agents guidance:", agents_assets_guidance, fence=fence)
        )
        consumed_guidance_characters += len(agents_assets_guidance) + _fenced_text_block_overhead(
            ".agents guidance:", agents_assets_guidance, fence=fence
        )
        guidance_block_count += 1

    if guidance_lines:
        # Account for "## Review Guidance" header and the newline after it
        consumed_guidance_characters += len("## Review Guidance") + 1
        if guidance_block_count > 1:
            # Account for newlines between multiple blocks joined by "\n"
            consumed_guidance_characters += guidance_block_count - 1

    review_guidance = read_workspace_review_guidance(
        workspace_path,
        max_characters=max(total_guidance_budget - consumed_guidance_characters, 0),
    )
    if review_guidance is not None:
        if guidance_block_count == 0:
            consumed_guidance_characters += len("## Review Guidance") + 1
        else:
            consumed_guidance_characters += 1  # separator between blocks

        # Budget math for N guidance blocks:
        # - Header separator: 1 \n (accounted for in consumed_guidance_characters += 1 above)
        # - Internal separators: 3 \n per block (accounted for in _fenced_text_block_overhead)
        # - Inter-block separators: 1 \n between blocks (accounted for in
        #   consumed_guidance_characters += 1 above)
        # Total newlines = 1 (header) + 3N (internal) + (N-1) (inter-block) = 4N.
        # This exactly matches the N*4 strings joined by \n in the final assembly.

        fence = markdown_fence_for_content(review_guidance)
        guidance_lines.extend(
            _fenced_text_block_lines("REVIEW.md guidance:", review_guidance, fence=fence)
        )
        consumed_guidance_characters += len(review_guidance) + _fenced_text_block_overhead(
            "REVIEW.md guidance:", review_guidance, fence=fence
        )
        guidance_block_count += 1

    build_test_context = _render_build_test_info(workspace_path)
    guidance_section = ""
    if guidance_lines:
        guidance_section = "\n".join(["## Review Guidance", *guidance_lines])

    task_lines = [
        "## Review Task",
        f"Reviewer kind: {reviewer_kind}",
    ]
    if task_text:
        task_lines.append(f"Task objective: {task_text}")
    task_lines.extend(
        [
            "Evaluate:",
            "1. Does the delivered diff satisfy the task objective?",
            "2. Are there unintended behavioral changes?",
            "3. Are there obvious logical issues?",
            "4. Are relevant tests or checks missing for changed behavior?",
        ]
    )

    schema_payload = {**_REVIEW_SCHEMA_PAYLOAD, "reviewer_kind": reviewer_kind}
    schema_json = json.dumps(schema_payload, indent=2)
    output_section = _REVIEW_OUTPUT_CONTRACT_TEMPLATE.format(schema_json=schema_json)

    sections = [
        _REVIEW_ROLE_SECTION,
        guidance_section,
        build_test_context or "",
        "\n".join(task_lines),
        f"## Review Context Packet\n{review_context_packet}"
        if review_context_packet.strip()
        else "",
        output_section,
    ]
    return "\n\n".join(section for section in sections if section.strip())


def build_system_prompt(
    request: WorkerRequest,
    workspace_path: Path,
    *,
    tool_registry: ToolRegistry | None = None,
    tool_client: McpToolClient | None = None,
) -> str:
    """Assemble the structured system prompt for a coding worker run."""
    agents_guidance_budget = DEFAULT_AGENTS_MAX_CHARACTERS
    agents_guidance, agents_assets_guidance = read_workspace_repo_guidance(
        workspace_path,
        max_characters=agents_guidance_budget,
    )
    guidance_wrapper_overhead = 0
    if agents_guidance is not None:
        guidance_wrapper_overhead += _fenced_text_block_overhead(
            "AGENTS.md guidance:", agents_guidance
        )
    if agents_assets_guidance is not None:
        guidance_wrapper_overhead += _fenced_text_block_overhead(
            ".agents guidance:", agents_assets_guidance
        )
    sections = [
        build_role_description_section(request),
        build_available_tools_section(tool_registry, tool_client),
        _build_repo_context_section_with_guidance(
            workspace_path,
            agents_guidance,
            agents_assets_guidance,
        ),
        _render_build_test_info(workspace_path) or "",
        build_task_context_section(request),
        build_workflow_instructions_section(request),
    ]
    return "\n\n".join(section for section in sections if section.strip())
