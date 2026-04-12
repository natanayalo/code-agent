"""Structured system prompt construction helpers for coding workers."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from tools import DEFAULT_MCP_TOOL_CLIENT, McpToolClient, ToolDefinition, ToolRegistry
from workers.base import WorkerRequest

DEFAULT_REPO_LISTING_MAX_DEPTH = 2
DEFAULT_REPO_LISTING_MAX_ENTRIES = 40
DEFAULT_AGENTS_MAX_CHARACTERS = 6000
DEFAULT_AGENTS_ASSET_READ_MAX_CHARACTERS = 8192
_TRUNCATED_MARKER = "\n... (truncated)"
_AGENTS_ASSET_DIRECTORIES = ("skills", "workflows", "rules")
_BUILD_CONTEXT_FILE_READ_MAX_CHARACTERS = 1048576
_BUILD_CONTEXT_ITEM_LIMIT = 8
_BUILD_CONTEXT_VALUE_MAX_CHARACTERS = 220
_WORKFLOW_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_COMMAND_HINT_PATTERN = re.compile(
    r"\b("
    r"pytest|pre-commit|ruff|mypy|tox|nox|make|npm|pnpm|yarn|docker compose|uv run|poetry run"
    r")\b"
)
_SKIPPED_PATH_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}


def build_role_description_section() -> str:
    """Describe the worker's job and guardrails."""
    return "\n".join(
        [
            "## Role",
            "You are the coding execution worker for code-agent.",
            "Work inside the checked-out repository, make the smallest safe changes that satisfy",
            "the task, and keep your reasoning grounded in the files and command output available",
            "inside the workspace.",
            "Do not mutate orchestrator state directly, write to memory directly, bypass sandbox",
            "rules, or invent results you did not verify.",
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
        return "\n".join(["## Available Tools", "- No tools configured."])
    tool_sections = [_render_tool_definition(tool) for tool in tools]
    return "\n\n".join(["## Available Tools", *tool_sections])


def _render_tool_definition(tool: ToolDefinition) -> str:
    """Render one tool definition for prompt injection."""
    expected_artifacts = (
        ", ".join(f"`{artifact.value}`" for artifact in tool.expected_artifacts)
        if tool.expected_artifacts
        else "`none`"
    )
    return "\n".join(
        [
            f"### `{tool.name}`",
            tool.description,
            f"- Capability category: `{tool.capability_category.value}`",
            f"- Side effect level: `{tool.side_effect_level.value}`",
            f"- Required permission: `{tool.required_permission.value}`",
            f"- Default timeout: `{tool.timeout_seconds}s`",
            f"- Network required: `{'yes' if tool.network_required else 'no'}`",
            f"- Deterministic: `{'yes' if tool.deterministic else 'no'}`",
            f"- Expected artifacts: {expected_artifacts}",
        ]
    )


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
        lines.extend(
            [
                "AGENTS.md guidance:",
                "```text",
                agents_guidance,
                "```",
            ]
        )
    if agents_assets_guidance is not None:
        lines.extend(
            [
                ".agents guidance:",
                "```text",
                agents_assets_guidance,
                "```",
            ]
        )
    return "\n".join(lines)


def _compact_json_summary(value: Any) -> str:
    """Render JSON data into one bounded line for prompt summaries."""
    serialized = json.dumps(_json_safe(value), sort_keys=True)
    return _truncate_to_budget(serialized, max_characters=_BUILD_CONTEXT_VALUE_MAX_CHARACTERS)


def _extract_makefile_targets(contents: str) -> list[str]:
    """Extract a bounded set of non-special Makefile targets."""
    targets: list[str] = []
    for line in contents.splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!\s*=)", line)
        if match is None:
            continue
        target = match.group(1)
        if target.startswith(".") or "%" in target:
            continue
        if target not in targets:
            targets.append(target)
        if len(targets) >= _BUILD_CONTEXT_ITEM_LIMIT:
            break
    return targets


def _summarize_makefile(workspace_path: Path) -> str | None:
    """Summarize actionable Makefile targets when available."""
    makefile_path = workspace_path / "Makefile"
    if not makefile_path.is_file():
        return None
    try:
        contents = _read_text_prefix(
            makefile_path,
            max_characters=_BUILD_CONTEXT_FILE_READ_MAX_CHARACTERS,
        )
    except OSError:
        return None
    targets = _extract_makefile_targets(contents)
    if not targets:
        return None
    joined_targets = ", ".join(targets)
    return f"- Makefile targets: {joined_targets}"


def _summarize_package_scripts(workspace_path: Path) -> str | None:
    """Summarize package.json scripts in a compact, actionable form."""
    package_json_path = workspace_path / "package.json"
    if not package_json_path.is_file():
        return None
    try:
        payload = json.loads(
            _read_text_prefix(
                package_json_path,
                max_characters=_BUILD_CONTEXT_FILE_READ_MAX_CHARACTERS,
            )
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict) or not scripts:
        return None

    rendered_pairs: list[str] = []
    for script_name in sorted(scripts):
        script_command = scripts[script_name]
        if not isinstance(script_name, str) or not isinstance(script_command, str):
            continue
        rendered_pairs.append(f"{script_name}={json.dumps(script_command)}")
        if len(rendered_pairs) >= _BUILD_CONTEXT_ITEM_LIMIT:
            break
    if not rendered_pairs:
        return None
    return f"- package.json scripts: {', '.join(rendered_pairs)}"


def _summarize_pyproject_config(workspace_path: Path) -> list[str]:
    """Extract concise build/test hints from pyproject.toml sections."""
    pyproject_path = workspace_path / "pyproject.toml"
    if not pyproject_path.is_file():
        return []
    try:
        payload = tomllib.loads(
            _read_text_prefix(
                pyproject_path,
                max_characters=_BUILD_CONTEXT_FILE_READ_MAX_CHARACTERS,
            )
        )
    except (OSError, tomllib.TOMLDecodeError):
        return []
    if not isinstance(payload, dict):
        return []

    lines: list[str] = []
    project_table = payload.get("project")
    if isinstance(project_table, dict):
        scripts = project_table.get("scripts")
        if isinstance(scripts, dict) and scripts:
            lines.append(f"- pyproject.toml [project.scripts]: {_compact_json_summary(scripts)}")

    tool_table = payload.get("tool")
    if not isinstance(tool_table, dict):
        return lines

    pytest_table = tool_table.get("pytest")
    if isinstance(pytest_table, dict):
        pytest_config = pytest_table.get("ini_options")
        if isinstance(pytest_config, dict):
            pytest_summary = _compact_json_summary(pytest_config)
            lines.append(f"- pyproject.toml [tool.pytest.ini_options]: {pytest_summary}")
        else:
            pytest_summary = _compact_json_summary(pytest_table)
            lines.append(f"- pyproject.toml [tool.pytest]: {pytest_summary}")

    ruff_table = tool_table.get("ruff")
    if isinstance(ruff_table, dict):
        lines.append(f"- pyproject.toml [tool.ruff]: {_compact_json_summary(ruff_table)}")

    mypy_table = tool_table.get("mypy")
    if isinstance(mypy_table, dict):
        lines.append(f"- pyproject.toml [tool.mypy]: {_compact_json_summary(mypy_table)}")

    return lines[:_BUILD_CONTEXT_ITEM_LIMIT]


def _parse_inline_yaml_key_values(raw_value: str) -> list[str]:
    """Parse simple inline YAML values into normalized key names."""
    stripped = _strip_yaml_comment(raw_value).strip()
    if not stripped:
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        candidates = _split_inline_yaml_list_values(stripped[1:-1])
    else:
        candidates = [stripped]
    keys: list[str] = []
    for candidate in candidates:
        normalized = candidate.strip("\"'")
        if normalized and _WORKFLOW_KEY_PATTERN.fullmatch(normalized):
            keys.append(normalized)
    return keys


def _strip_yaml_comment(raw_value: str) -> str:
    """Strip trailing YAML comments while respecting quoted text."""
    in_single_quote = False
    in_double_quote = False
    escaped = False
    result_chars: list[str] = []
    for char in raw_value:
        if escaped:
            result_chars.append(char)
            escaped = False
            continue
        if char == "\\" and in_double_quote:
            result_chars.append(char)
            escaped = True
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            result_chars.append(char)
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            result_chars.append(char)
            continue
        if char == "#" and not in_single_quote and not in_double_quote:
            break
        result_chars.append(char)
    return "".join(result_chars)


def _split_inline_yaml_list_values(raw_value: str) -> list[str]:
    """Split inline YAML list values on commas outside quotes."""
    values: list[str] = []
    current_chars: list[str] = []
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for char in raw_value:
        if escaped:
            current_chars.append(char)
            escaped = False
            continue
        if char == "\\" and in_double_quote:
            current_chars.append(char)
            escaped = True
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            current_chars.append(char)
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            current_chars.append(char)
            continue
        if char == "," and not in_single_quote and not in_double_quote:
            values.append("".join(current_chars).strip())
            current_chars = []
            continue
        current_chars.append(char)

    values.append("".join(current_chars).strip())
    return values


def _extract_yaml_top_level_keys(contents: str, *, root_key: str) -> list[str]:
    """Extract first-level keys from a simple YAML mapping block."""
    keys: list[str] = []
    lines = contents.splitlines()
    in_block = False
    root_indent = 0
    child_indent: int | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))

        if not in_block:
            key_candidate = stripped.partition(":")[0].strip().strip("\"'")
            if indent != 0 or ":" not in stripped or key_candidate != root_key:
                continue
            in_block = True
            root_indent = indent
            inline_value = stripped.partition(":")[2]
            keys.extend(_parse_inline_yaml_key_values(inline_value))
            continue

        if indent <= root_indent:
            break
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue

        candidate = stripped
        if candidate.startswith("-"):
            candidate = candidate[1:].strip()
        candidate = candidate.partition(":")[0].strip().strip("\"'")
        if not candidate or not _WORKFLOW_KEY_PATTERN.fullmatch(candidate):
            continue
        if candidate not in keys:
            keys.append(candidate)
        if len(keys) >= _BUILD_CONTEXT_ITEM_LIMIT:
            break
    return keys


def _summarize_github_workflows(workspace_path: Path) -> list[str]:
    """Summarize workflow trigger events and jobs from workflow YAML files."""
    workflows_path = workspace_path / ".github" / "workflows"
    if not workflows_path.is_dir():
        return []

    summaries: list[str] = []
    workflow_files = sorted(
        (
            file_path
            for file_path in workflows_path.iterdir()
            if file_path.is_file() and file_path.suffix.lower() in {".yml", ".yaml"}
        ),
        key=lambda file_path: file_path.name.lower(),
    )
    for workflow_file in workflow_files[:_BUILD_CONTEXT_ITEM_LIMIT]:
        try:
            contents = _read_text_prefix(
                workflow_file,
                max_characters=_BUILD_CONTEXT_FILE_READ_MAX_CHARACTERS,
            )
        except OSError:
            continue
        events = _extract_yaml_top_level_keys(contents, root_key="on")
        jobs = _extract_yaml_top_level_keys(contents, root_key="jobs")
        summary_parts: list[str] = []
        if events:
            summary_parts.append(f"on={', '.join(events)}")
        if jobs:
            summary_parts.append(f"jobs={', '.join(jobs)}")
        if summary_parts:
            summaries.append(
                f"- .github/workflows/{workflow_file.name}: {'; '.join(summary_parts)}"
            )
    return summaries


def _normalize_command_hint(raw_line: str) -> str | None:
    """Normalize a potential command line from markdown content."""
    normalized = raw_line.strip().lstrip("-*").strip()
    if not normalized:
        return None
    if normalized.startswith("`") and normalized.endswith("`") and len(normalized) > 1:
        normalized = normalized[1:-1].strip()
    if not normalized:
        return None
    if _COMMAND_HINT_PATTERN.search(normalized) is None and not normalized.startswith(".venv/bin/"):
        return None
    return normalized


def _summarize_contributing_commands(workspace_path: Path) -> str | None:
    """Summarize actionable command hints from CONTRIBUTING.md."""
    contributing_path = workspace_path / "CONTRIBUTING.md"
    if not contributing_path.is_file():
        return None
    try:
        contents = _read_text_prefix(
            contributing_path,
            max_characters=_BUILD_CONTEXT_FILE_READ_MAX_CHARACTERS,
        )
    except OSError:
        return None

    command_hints: list[str] = []
    for line in contents.splitlines():
        normalized = _normalize_command_hint(line)
        if normalized is None or normalized in command_hints:
            continue
        command_hints.append(normalized)
        if len(command_hints) >= _BUILD_CONTEXT_ITEM_LIMIT:
            break
    if not command_hints:
        return None
    return f"- CONTRIBUTING.md commands: {'; '.join(command_hints)}"


def _summarize_dockerfile(workspace_path: Path) -> str | None:
    """Summarize base image and startup hints from Dockerfile."""
    dockerfile_path = workspace_path / "Dockerfile"
    if not dockerfile_path.is_file():
        return None
    try:
        contents = _read_text_prefix(
            dockerfile_path,
            max_characters=_BUILD_CONTEXT_FILE_READ_MAX_CHARACTERS,
        )
    except OSError:
        return None

    base_image: str | None = None
    entrypoint: str | None = None
    cmd: str | None = None
    for line in _combine_dockerfile_logical_lines(contents):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        instruction_parts = stripped.split(None, 1)
        if len(instruction_parts) < 2:
            continue
        instruction = instruction_parts[0].upper()
        payload = instruction_parts[1].strip()
        if not payload:
            continue

        if instruction == "FROM":
            base_candidate = payload
            if base_candidate:
                base_image = base_candidate
        elif instruction == "ENTRYPOINT":
            entrypoint_candidate = payload
            if entrypoint_candidate:
                entrypoint = entrypoint_candidate
        elif instruction == "CMD":
            cmd_candidate = payload
            if cmd_candidate:
                cmd = cmd_candidate

    parts: list[str] = []
    if base_image is not None:
        parts.append(f"base={base_image}")
    if entrypoint is not None:
        parts.append(f"entrypoint={_truncate_to_budget(entrypoint, max_characters=80)}")
    if cmd is not None:
        parts.append(f"cmd={_truncate_to_budget(cmd, max_characters=80)}")
    if not parts:
        return None
    return f"- Dockerfile: {'; '.join(parts)}"


def _combine_dockerfile_logical_lines(contents: str) -> list[str]:
    """Combine Dockerfile continuation lines into logical instruction lines."""
    logical_lines: list[str] = []
    current: str | None = None

    for raw_line in contents.splitlines():
        line = raw_line.rstrip()
        if current is None:
            current = line
        else:
            current = f"{current} {line.lstrip()}"
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        logical_lines.append(current)
        current = None

    if current is not None:
        logical_lines.append(current)
    return logical_lines


def build_build_test_section(
    workspace_path: Path,
    *,
    max_characters: int = DEFAULT_AGENTS_MAX_CHARACTERS,
) -> str | None:
    """Render bounded build/test/CI context from common repo config sources."""
    if max_characters <= 0:
        return None

    lines: list[str] = []
    makefile_summary = _summarize_makefile(workspace_path)
    if makefile_summary is not None:
        lines.append(makefile_summary)

    package_summary = _summarize_package_scripts(workspace_path)
    if package_summary is not None:
        lines.append(package_summary)

    lines.extend(_summarize_pyproject_config(workspace_path))
    lines.extend(_summarize_github_workflows(workspace_path))

    contributing_summary = _summarize_contributing_commands(workspace_path)
    if contributing_summary is not None:
        lines.append(contributing_summary)

    dockerfile_summary = _summarize_dockerfile(workspace_path)
    if dockerfile_summary is not None:
        lines.append(dockerfile_summary)

    if not lines:
        return None
    section = "\n".join(["## Build & Test", *lines])
    return _truncate_to_budget(section, max_characters=max_characters)


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
        f"Task text: {request.task_text}",
        f"Session ID: {request.session_id or 'unknown'}",
        f"Repository URL: {_mask_repo_url(request.repo_url) or 'not provided'}",
        f"Branch: {request.branch or 'default'}",
    ]
    if request.memory_context:
        lines.extend(
            [
                "Memory context:",
                "```json",
                _render_json_block(request.memory_context),
                "```",
            ]
        )
    if request.constraints:
        lines.extend(
            [
                "Constraints:",
                "```json",
                _render_json_block(request.constraints),
                "```",
            ]
        )
    if request.budget:
        lines.extend(
            [
                "Budget:",
                "```json",
                _render_json_block(request.budget),
                "```",
            ]
        )
    return "\n".join(lines)


def build_workflow_instructions_section() -> str:
    """Describe the expected worker execution workflow."""
    return "\n".join(
        [
            "## Workflow Instructions",
            "- Inspect relevant files before making changes.",
            "- Prefer minimal, reviewable edits over broad rewrites.",
            "- The persistent shell already starts in the checked-out workspace repository; "
            "treat `repo_url` as the clone source, not as a filesystem path to `cd` into.",
            "- Use the available tools with focused commands and targeted reads; avoid dumping "
            "large files or verbose output, and narrow long or truncated results with `rg`, "
            "`sed -n`, `head`, `tail`, or focused test selectors before continuing.",
            "- If a command fails, inspect the current files, paths, and assumptions before "
            "retrying; do not blindly repeat the same command.",
            "- Base the next step on command exit codes and the relevant output you actually "
            "observed.",
            "- Surface blockers or missing prerequisites explicitly instead of guessing.",
            "- End with a concise summary of changes, verification, and any follow-up needed.",
        ]
    )


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
    consumed_guidance_characters = len(agents_guidance or "") + len(agents_assets_guidance or "")
    build_section = build_build_test_section(
        workspace_path,
        max_characters=max(agents_guidance_budget - consumed_guidance_characters, 0),
    )

    sections = [
        build_role_description_section(),
        build_available_tools_section(tool_registry, tool_client),
        _build_repo_context_section_with_guidance(
            workspace_path,
            agents_guidance,
            agents_assets_guidance,
        ),
        build_section or "",
        build_task_context_section(request),
        build_workflow_instructions_section(),
    ]
    return "\n\n".join(section for section in sections if section.strip())
