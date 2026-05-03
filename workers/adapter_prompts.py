"""Shared prompt-rendering helpers for runtime adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from workers.cli_runtime import CliRuntimeMessage, CliRuntimeStep

DEFAULT_TOOL_SELECTION_RULE = (
    "- Use only tool names listed in the system prompt's Available Tools section."
)
DEFAULT_TOOL_INPUT_RULE = (
    "- `tool_input` MUST be a string. If the tool expects JSON, encode that JSON as a string."
)
DEFAULT_TOOL_INPUT_ENCODE_RULE = (
    "- `tool_input` MUST be a string. If the tool expects JSON, you must encode "
    "that JSON as a string inside the tool_input field."
)
DEFAULT_STRICT_KIND_RULE = (
    "- `kind` must be EXACTLY 'tool_call' or 'final'. NEVER use other values like 'tool_code'."
)
DEFAULT_SUFFICIENT_CONTEXT_RULE = (
    "- If the transcript already contains enough information to finish, return `final`."
)
DEFAULT_ADAPT_FAILURE_RULE = (
    "- If the latest tool result failed, adapt to that failure instead of repeating blindly."
)
DEFAULT_RAW_JSON_ONLY_RULE = (
    "- Return ONLY a raw JSON object. No markdown fences, no extra explanation."
)
CODEX_ADAPTER_IDENTITY_LINE = "You are the Codex runtime adapter for a bounded coding worker."
GEMINI_ADAPTER_IDENTITY_LINE = "You are the Gemini runtime adapter for a bounded coding worker."
OPENROUTER_ADAPTER_IDENTITY_LINE = (
    "You are the OpenRouter runtime adapter for a bounded coding worker."
)
CODEX_SCHEMA_RESPONSE_LINE = (
    "Read the transcript below and return exactly one JSON object matching the provided schema."
)
_JSON_ONLY_RESPONSE_SUFFIX = (
    "exactly one JSON object with no surrounding text, no markdown fences, and no explanation."
)


def render_message_heading(message: CliRuntimeMessage, *, index: int) -> str:
    """Render a compact transcript heading for one runtime message."""
    if message.role == "tool":
        return f"### Message {index} ({message.role}:{message.tool_name})"
    return f"### Message {index} ({message.role})"


def render_runtime_transcript_lines(
    messages: Sequence[CliRuntimeMessage],
    *,
    content_transform: Callable[[object], str] | None = None,
) -> list[str]:
    """Render transcript lines for runtime adapter prompts."""
    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        content = (
            content_transform(message.content) if content_transform is not None else message.content
        )
        lines.extend((render_message_heading(message, index=index), content, ""))
    return lines


def render_two_action_lines(
    *,
    tool_call_example: str,
    final_example: str,
    action_intro: str = "Choose one of two actions:",
) -> list[str]:
    """Render the common two-action example section."""
    return [
        action_intro,
        tool_call_example,
        final_example,
    ]


def build_tool_call_example_json(
    *,
    tool_input: str = "<tool input string>",
    exclude_none: bool = False,
) -> str:
    """Build a canonical `tool_call` example JSON for adapter prompts."""
    return CliRuntimeStep(
        kind="tool_call",
        tool_name="<registered tool name>",
        tool_input=tool_input,
        final_output=None,
    ).model_dump_json(exclude_none=exclude_none)


def build_final_example_json(
    *,
    final_output: str = "<final summary for the user>",
    exclude_none: bool = False,
) -> str:
    """Build a canonical `final` example JSON for adapter prompts."""
    return CliRuntimeStep(
        kind="final",
        final_output=final_output,
        tool_name=None,
        tool_input=None,
    ).model_dump_json(exclude_none=exclude_none)


def render_rules_lines(
    tool_guidance_lines: Sequence[str],
    *,
    json_output_rule: str,
    tool_input_rule: str = DEFAULT_TOOL_INPUT_RULE,
    pre_rules_lines: Sequence[str] = (),
) -> list[str]:
    """Render common adapter decision rules with optional adapter-specific pre-rules."""
    return [
        "Rules:",
        *pre_rules_lines,
        DEFAULT_TOOL_SELECTION_RULE,
        tool_input_rule,
        *tool_guidance_lines,
        DEFAULT_SUFFICIENT_CONTEXT_RULE,
        DEFAULT_ADAPT_FAILURE_RULE,
        json_output_rule,
    ]


def append_worker_system_prompt_section(lines: list[str], system_prompt: str | None) -> None:
    """Append worker-system-prompt section when prompt content is present."""
    if system_prompt is None or not system_prompt.strip():
        return
    lines.extend(
        [
            "",
            "## Worker System Prompt",
            system_prompt.strip(),
        ]
    )


def append_runtime_transcript_section(
    lines: list[str],
    messages: Sequence[CliRuntimeMessage],
    *,
    content_transform: Callable[[object], str] | None = None,
) -> None:
    """Append runtime transcript heading and message blocks."""
    lines.extend(["", "## Runtime Transcript"])
    lines.extend(
        render_runtime_transcript_lines(
            messages,
            content_transform=content_transform,
        )
    )


def build_runtime_transcript_prompt(
    *,
    identity_line: str,
    response_instruction_line: str,
    tool_call_example: str,
    final_example: str,
    tool_guidance_lines: Sequence[str],
    messages: Sequence[CliRuntimeMessage],
    system_prompt: str | None = None,
    json_output_rule: str,
    tool_input_rule: str = DEFAULT_TOOL_INPUT_RULE,
    pre_rules_lines: Sequence[str] = (),
    action_intro: str = "Choose one of two actions:",
    content_transform: Callable[[object], str] | None = None,
) -> str:
    """Build a complete runtime adapter prompt for transcript-driven turns."""
    lines = [
        identity_line,
        response_instruction_line,
    ]
    lines.extend(
        render_two_action_lines(
            tool_call_example=tool_call_example,
            final_example=final_example,
            action_intro=action_intro,
        )
    )
    lines.extend(
        render_rules_lines(
            tool_guidance_lines,
            json_output_rule=json_output_rule,
            tool_input_rule=tool_input_rule,
            pre_rules_lines=pre_rules_lines,
        )
    )
    append_worker_system_prompt_section(lines, system_prompt)
    append_runtime_transcript_section(
        lines,
        messages,
        content_transform=content_transform,
    )
    return "\n".join(lines).rstrip()


def json_only_response_line(*, include_transcript_reference: bool) -> str:
    """Render a standard JSON-only response instruction line."""
    if include_transcript_reference:
        return f"Read the transcript below and return {_JSON_ONLY_RESPONSE_SUFFIX}"
    return f"Return {_JSON_ONLY_RESPONSE_SUFFIX}"


def build_role_native_system_instructions(
    *,
    identity_line: str,
    json_only_response_line: str,
    tool_call_example: str,
    final_example: str,
    tool_guidance_lines: Sequence[str],
    system_prompt: str | None = None,
    json_output_rule: str = DEFAULT_RAW_JSON_ONLY_RULE,
    tool_input_rule: str = DEFAULT_TOOL_INPUT_RULE,
    pre_rules_lines: Sequence[str] = (),
    action_intro: str = "Choose one of two actions.",
    examples_header: str = "Examples:",
    tool_example_label: str = "Example tool_call:",
    final_example_label: str = "Example final:",
) -> str:
    """Build reusable role-native instruction text for chat adapters."""
    lines = [
        identity_line,
        json_only_response_line,
        action_intro,
        examples_header,
        tool_example_label,
        tool_call_example,
        final_example_label,
        final_example,
    ]
    lines.extend(
        render_rules_lines(
            tool_guidance_lines,
            json_output_rule=json_output_rule,
            tool_input_rule=tool_input_rule,
            pre_rules_lines=pre_rules_lines,
        )
    )
    append_worker_system_prompt_section(lines, system_prompt)
    return "\n".join(lines)


def build_override_system_instructions(
    *,
    identity_line: str,
    json_only_response_line: str,
    follow_user_rule: str,
    system_prompt: str | None = None,
) -> str:
    """Build override-safe system instructions for direct prompt overrides."""
    lines = [
        identity_line,
        json_only_response_line,
        follow_user_rule,
    ]
    append_worker_system_prompt_section(lines, system_prompt)
    return "\n".join(lines)
