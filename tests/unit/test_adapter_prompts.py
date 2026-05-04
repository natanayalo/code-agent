"""Unit tests for shared adapter prompt helpers."""

from __future__ import annotations

from workers.adapter_prompts import (
    CODEX_ADAPTER_IDENTITY_LINE,
    CODEX_SCHEMA_RESPONSE_LINE,
    DEFAULT_ADAPT_FAILURE_RULE,
    DEFAULT_RAW_JSON_ONLY_RULE,
    DEFAULT_SUFFICIENT_CONTEXT_RULE,
    DEFAULT_TOOL_SELECTION_RULE,
    GEMINI_ADAPTER_IDENTITY_LINE,
    OPENROUTER_ADAPTER_IDENTITY_LINE,
    append_runtime_transcript_section,
    append_worker_system_prompt_section,
    build_final_example_json,
    build_override_system_instructions,
    build_role_native_system_instructions,
    build_runtime_transcript_prompt,
    build_tool_call_example_json,
    json_only_response_line,
    render_message_heading,
    render_rules_lines,
    render_runtime_transcript_lines,
    render_two_action_lines,
)
from workers.cli_runtime import CliRuntimeMessage


def test_render_message_heading_formats_roles() -> None:
    """Heading renderer should format tool and non-tool roles consistently."""
    system_message = CliRuntimeMessage(role="system", content="Rules.")
    tool_message = CliRuntimeMessage(role="tool", tool_name="execute_bash", content="Output.")

    assert render_message_heading(system_message, index=1) == "### Message 1 (system)"
    assert render_message_heading(tool_message, index=2) == "### Message 2 (tool:execute_bash)"


def test_render_runtime_transcript_lines_default_content() -> None:
    """Transcript renderer should emit heading/content/blank blocks per message."""
    messages = [
        CliRuntimeMessage(role="assistant", content="Step one."),
        CliRuntimeMessage(role="tool", tool_name="execute_bash", content="Exit code: 0"),
    ]

    lines = render_runtime_transcript_lines(messages)

    assert lines == [
        "### Message 1 (assistant)",
        "Step one.",
        "",
        "### Message 2 (tool:execute_bash)",
        "Exit code: 0",
        "",
    ]


def test_render_runtime_transcript_lines_applies_content_transform() -> None:
    """Optional content transform should be applied to each message content."""
    messages = [CliRuntimeMessage(role="system", content="hello")]

    lines = render_runtime_transcript_lines(
        messages,
        content_transform=lambda value: str(value).upper(),
    )

    assert lines == ["### Message 1 (system)", "HELLO", ""]


def test_render_two_action_lines_uses_default_intro() -> None:
    """Two-action renderer should keep default heading and examples order."""
    lines = render_two_action_lines(
        tool_call_example='{"kind":"tool_call"}',
        final_example='{"kind":"final"}',
    )

    assert lines == [
        "Choose one of two actions:",
        '{"kind":"tool_call"}',
        '{"kind":"final"}',
    ]


def test_build_tool_call_example_json_defaults() -> None:
    """Tool-call example helper should produce the canonical JSON scaffold."""
    assert build_tool_call_example_json() == (
        '{"kind":"tool_call","tool_name":"<registered tool name>",'
        '"tool_input":"<tool input string>","final_output":null}'
    )
    assert build_tool_call_example_json(exclude_none=True) == (
        '{"kind":"tool_call","tool_name":"<registered tool name>",'
        '"tool_input":"<tool input string>"}'
    )


def test_build_final_example_json_defaults() -> None:
    """Final example helper should produce canonical with optional null omission."""
    assert build_final_example_json() == (
        '{"kind":"final","tool_name":null,"tool_input":null,'
        '"final_output":"<final summary for the user>"}'
    )
    assert build_final_example_json(exclude_none=True) == (
        '{"kind":"final","final_output":"<final summary for the user>"}'
    )


def test_render_rules_lines_supports_overrides() -> None:
    """Rule renderer should merge common rules with adapter-specific customizations."""
    lines = render_rules_lines(
        ["- Tool guidance: prefer execute_bash for shell commands."],
        json_output_rule=DEFAULT_RAW_JSON_ONLY_RULE,
        tool_input_rule="- `tool_input` MUST be encoded as JSON text.",
        pre_rules_lines=("- `kind` must be EXACTLY 'tool_call' or 'final'.",),
    )

    assert lines[0] == "Rules:"
    assert lines[1] == "- `kind` must be EXACTLY 'tool_call' or 'final'."
    assert DEFAULT_TOOL_SELECTION_RULE in lines
    assert "- `tool_input` MUST be encoded as JSON text." in lines
    assert "- Tool guidance: prefer execute_bash for shell commands." in lines
    assert DEFAULT_SUFFICIENT_CONTEXT_RULE in lines
    assert DEFAULT_ADAPT_FAILURE_RULE in lines
    assert lines[-1] == DEFAULT_RAW_JSON_ONLY_RULE


def test_append_worker_system_prompt_section_appends_when_present() -> None:
    """Helper should append worker prompt section only when prompt text is non-empty."""
    lines = ["base"]
    append_worker_system_prompt_section(lines, " Follow coding rules. ")

    assert lines == ["base", "", "## Worker System Prompt", "Follow coding rules."]


def test_append_worker_system_prompt_section_noop_for_blank_input() -> None:
    """Helper should not modify lines for empty or missing system prompts."""
    lines = ["base"]
    append_worker_system_prompt_section(lines, "   ")
    append_worker_system_prompt_section(lines, None)

    assert lines == ["base"]


def test_append_runtime_transcript_section_appends_heading_and_messages() -> None:
    """Transcript appender should add heading and rendered message blocks."""
    lines = ["base"]
    messages = [CliRuntimeMessage(role="assistant", content="Step one.")]

    append_runtime_transcript_section(lines, messages)

    assert lines == [
        "base",
        "",
        "## Runtime Transcript",
        "### Message 1 (assistant)",
        "Step one.",
        "",
    ]


def test_append_runtime_transcript_section_applies_transform() -> None:
    """Transcript appender should honor optional content transform."""
    lines: list[str] = []
    messages = [CliRuntimeMessage(role="system", content="hello")]

    append_runtime_transcript_section(
        lines,
        messages,
        content_transform=lambda value: str(value).upper(),
    )

    assert lines == [
        "",
        "## Runtime Transcript",
        "### Message 1 (system)",
        "HELLO",
        "",
    ]


def test_build_runtime_transcript_prompt_shape() -> None:
    """Shared runtime prompt builder should include rules and transcript sections."""
    instructions = build_runtime_transcript_prompt(
        identity_line="Identity",
        response_instruction_line="Return exactly one JSON object.",
        tool_call_example='{"kind":"tool_call"}',
        final_example='{"kind":"final"}',
        tool_guidance_lines=("- Guidance line.",),
        messages=[CliRuntimeMessage(role="assistant", content="Step one.")],
        system_prompt="Keep responses short.",
        json_output_rule=DEFAULT_RAW_JSON_ONLY_RULE,
    )

    assert "Identity" in instructions
    assert "Return exactly one JSON object." in instructions
    assert "Choose one of two actions:" in instructions
    assert "Rules:" in instructions
    assert "- Guidance line." in instructions
    assert "## Worker System Prompt" in instructions
    assert "Keep responses short." in instructions
    assert "## Runtime Transcript" in instructions
    assert "### Message 1 (assistant)" in instructions


def test_adapter_identity_constants() -> None:
    """Shared identity/response constants should preserve adapter preambles."""
    assert CODEX_ADAPTER_IDENTITY_LINE == (
        "You are the Codex runtime adapter for a bounded coding worker."
    )
    assert GEMINI_ADAPTER_IDENTITY_LINE == (
        "You are the Gemini runtime adapter for a bounded coding worker."
    )
    assert OPENROUTER_ADAPTER_IDENTITY_LINE == (
        "You are the OpenRouter runtime adapter for a bounded coding worker."
    )
    assert CODEX_SCHEMA_RESPONSE_LINE == (
        "Read the transcript below and return exactly one JSON object matching the provided schema."
    )


def test_json_only_response_line_variants() -> None:
    """JSON-only helper should preserve both variants."""
    assert OPENROUTER_ADAPTER_IDENTITY_LINE == (
        "You are the OpenRouter runtime adapter for a bounded coding worker."
    )
    assert json_only_response_line(include_transcript_reference=True) == (
        "Read the transcript below and return exactly one JSON object with no surrounding text, "
        "no markdown fences, and no explanation."
    )
    assert json_only_response_line(include_transcript_reference=False) == (
        "Return exactly one JSON object with no surrounding text, no markdown fences, "
        "and no explanation."
    )


def test_build_role_native_system_instructions_shape() -> None:
    """Role-native instruction helper should include examples, rules, and worker prompt."""
    instructions = build_role_native_system_instructions(
        identity_line="Identity",
        json_only_response_line="Return JSON only.",
        tool_call_example='{"kind":"tool_call","tool_name":"execute_bash","tool_input":"ls -la"}',
        final_example='{"kind":"final","final_output":"done"}',
        tool_guidance_lines=("- Guidance line.",),
        system_prompt="Keep responses short.",
        json_output_rule=DEFAULT_RAW_JSON_ONLY_RULE,
    )

    assert "Identity" in instructions
    assert "Return JSON only." in instructions
    assert "Choose one of two actions." in instructions
    assert "Examples:" in instructions
    assert "Example tool_call:" in instructions
    assert "Example final:" in instructions
    assert "- Guidance line." in instructions
    assert "## Worker System Prompt" in instructions
    assert "Keep responses short." in instructions


def test_build_override_system_instructions_shape() -> None:
    """Override helper should preserve identity/json lines and append worker prompt."""
    instructions = build_override_system_instructions(
        identity_line="Identity",
        json_only_response_line="Return JSON only.",
        follow_user_rule="Follow the user schema instructions.",
        system_prompt="Keep responses short.",
    )

    assert "Identity" in instructions
    assert "Return JSON only." in instructions
    assert "Follow the user schema instructions." in instructions
    assert "## Worker System Prompt" in instructions
    assert "Keep responses short." in instructions
