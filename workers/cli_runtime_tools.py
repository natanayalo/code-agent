"""Tool parsing and transcript helpers for the shared CLI runtime."""

from __future__ import annotations

import json

from sandbox import DockerShellCommandResult
from sandbox.redact import SecretRedactor, mask_url_credentials, sanitize_command
from tools import (
    EXECUTE_BROWSER_TOOL_NAME,
    EXECUTE_GIT_TOOL_NAME,
    EXECUTE_GITHUB_TOOL_NAME,
    SEARCH_DIR_TOOL_NAME,
    SEARCH_FILE_TOOL_NAME,
    STR_REPLACE_EDITOR_TOOL_NAME,
    VIEW_FILE_TOOL_NAME,
    ToolDefinition,
    build_browser_command_from_input,
    build_git_command_from_input,
    build_github_command_from_input,
    build_search_dir_command_from_input,
    build_search_file_command_from_input,
    build_str_replace_editor_command_from_input,
    build_view_file_command_from_input,
)
from workers.adapter_utils import truncate_detail_keep_tail
from workers.cli_runtime_context import _truncate_text
from workers.cli_runtime_types import CliRuntimeStep

TOOL_NAME_ALIASES: dict[str, str] = {
    "functions.exec_command": "execute_bash",
    "exec_command": "execute_bash",
    "bash": "execute_bash",
    "run_shell_command": "execute_bash",
}


def _format_expected_artifacts(tool: ToolDefinition) -> str:
    """Render expected tool artifacts for prompt/runtime transcripts."""
    if not tool.expected_artifacts:
        return "none"
    return ", ".join(artifact.value for artifact in tool.expected_artifacts)


def _normalize_requested_tool_name(tool_name: str) -> str:
    """Map known adapter/runtime aliases onto registered tool names."""
    normalized = tool_name.strip()
    if not normalized:
        return tool_name
    return TOOL_NAME_ALIASES.get(normalized, normalized)


def _extract_first_json_object(text: str) -> str | None:
    """Extract the first syntactically valid JSON object from free-form text."""
    stripped = text.strip()
    search_from = 0
    while True:
        start = stripped.find("{", search_from)
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape_next = False
        end = -1
        for index, char in enumerate(stripped[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if in_string:
                if char == "\\":
                    escape_next = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            return None
        candidate = stripped[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            search_from = end + 1


def _parse_runtime_step_from_text(text: str) -> CliRuntimeStep | None:
    """Attempt to parse an embedded CliRuntimeStep payload from text content."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return CliRuntimeStep.model_validate_json(stripped)
    except Exception:
        pass
    candidate = _extract_first_json_object(stripped)
    if candidate is None:
        return None
    try:
        return CliRuntimeStep.model_validate_json(candidate)
    except Exception:
        return None


def _looks_like_tool_call_payload_text(text: str) -> bool:
    """Heuristically detect tool_call payload text even when JSON is malformed."""
    lowered = text.lower()
    return (
        '"kind"' in lowered
        and '"tool_call"' in lowered
        and '"tool_name"' in lowered
        and '"tool_input"' in lowered
    )


def _format_unsupported_tool_observation(
    *,
    tool_name: str,
    max_characters: int,
) -> str:
    """Render a recoverable observation for adapter-only control tools."""
    guidance = (
        "Tool is unavailable in this runtime. Continue with registered tools only "
        "(for example, execute_bash, view_file, search_file, search_dir, str_replace_editor)."
    )
    content, _ = _truncate_text(guidance, max_characters=max_characters)
    return "\n".join(
        [
            f"Tool result: {tool_name}",
            "Status: unavailable_tool",
            "Error: tool is not registered in this runtime.",
            f"Guidance: {content}",
        ]
    )


def _tool_call_transcript(tool: ToolDefinition, command: str) -> str:
    """Render a compact assistant transcript entry for a tool call."""
    return "\n".join(
        [
            f"Tool call: {tool.name}",
            f"Required permission: {tool.required_permission.value}",
            f"Default timeout seconds: {tool.timeout_seconds}",
            f"Expected artifacts: {_format_expected_artifacts(tool)}",
            "```bash",
            command,
            "```",
        ]
    )


def format_tool_observation(
    result: DockerShellCommandResult,
    *,
    tool_name: str,
    max_characters: int,
    redactor: SecretRedactor | None = None,
) -> str:
    """Render bounded shell output for adapter follow-up turns."""
    sanitized = mask_url_credentials(result.output)
    if redactor:
        sanitized = redactor.redact(sanitized)

    output = truncate_detail_keep_tail(sanitized, max_characters=max_characters)

    lines = [
        f"Tool result: {tool_name}",
        f"Command: {sanitize_command(result.command, redactor)}",
        f"Exit code: {result.exit_code}",
        f"Duration seconds: {result.duration_seconds:.3f}",
        "Output:",
        "```text",
        output if (output and output != "<empty>") else "<no output>",
        "```",
    ]
    return "\n".join(lines)


def format_bash_observation(
    result: DockerShellCommandResult,
    *,
    max_characters: int,
) -> str:
    """Backward-compatible wrapper for bash observations."""
    return format_tool_observation(
        result,
        tool_name="execute_bash",
        max_characters=max_characters,
    )


def _format_invalid_tool_input_observation(
    *,
    tool_name: str,
    tool_input: str,
    error: str,
    max_characters: int,
) -> str:
    """Render recoverable tool-input validation feedback for the adapter."""
    raw_input, truncated = _truncate_text(tool_input, max_characters=max_characters)
    lines = [
        f"Tool result: {tool_name}",
        "Status: input_validation_failed",
        f"Error: {error}",
        "Raw tool_input:",
        "```text",
        raw_input or "<empty>",
        "```",
    ]
    if truncated:
        lines.append(f"[tool_input truncated to {max_characters} characters]")
    if tool_name == STR_REPLACE_EDITOR_TOOL_NAME:
        lines.append(
            "Guidance: for multiline edits, use `execute_bash` (for example, a heredoc rewrite); "
            "use `str_replace_editor` only for single-line old_text/new_text replacements."
        )
    return "\n".join(lines)


def _resolve_tool_command(tool: ToolDefinition, raw_input: str) -> str:
    """Normalize tool input into the concrete shell command executed in the sandbox."""
    command = raw_input.strip()
    if tool.name == VIEW_FILE_TOOL_NAME:
        return build_view_file_command_from_input(command)
    if tool.name == SEARCH_FILE_TOOL_NAME:
        return build_search_file_command_from_input(command)
    if tool.name == SEARCH_DIR_TOOL_NAME:
        return build_search_dir_command_from_input(command)
    if tool.name == STR_REPLACE_EDITOR_TOOL_NAME:
        return build_str_replace_editor_command_from_input(command)
    if tool.name == EXECUTE_BROWSER_TOOL_NAME:
        return build_browser_command_from_input(
            command,
            timeout_seconds=tool.timeout_seconds,
        )
    if tool.name == EXECUTE_GIT_TOOL_NAME:
        return build_git_command_from_input(command)
    if tool.name == EXECUTE_GITHUB_TOOL_NAME:
        return build_github_command_from_input(command)
    return command
