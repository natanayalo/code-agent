"""Concrete OpenRouter adapter using the OpenAI-compatible API client."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from workers.adapter_messages import (
    build_role_native_transcript,
    serialize_openai_compatible_messages,
)
from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep
from workers.prompt import build_runtime_adapter_tool_guidance_lines

DEFAULT_OPENROUTER_BASE_URL: Final[str] = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL: Final[str] = "anthropic/claude-3.5-sonnet"
DEFAULT_OPENROUTER_REQUEST_TIMEOUT_SECONDS: Final[int] = 120
DEFAULT_OPENROUTER_HTTP_REFERER: Final[str] = "https://github.com/natanayalo/code-agent"
DEFAULT_OPENROUTER_X_TITLE: Final[str] = "code-agent"
_DETAIL_PREVIEW_CHARACTERS: Final[int] = 1200

OPENROUTER_API_KEY_ENV_VAR: Final[str] = "OPENROUTER_API_KEY"
OPENROUTER_MODEL_ENV_VAR: Final[str] = "CODE_AGENT_OPENROUTER_MODEL"
OPENROUTER_TIMEOUT_ENV_VAR: Final[str] = "CODE_AGENT_OPENROUTER_TIMEOUT_SECONDS"
OPENROUTER_HTTP_REFERER_ENV_VAR: Final[str] = "CODE_AGENT_OPENROUTER_HTTP_REFERER"
OPENROUTER_X_TITLE_ENV_VAR: Final[str] = "CODE_AGENT_OPENROUTER_X_TITLE"
OPENROUTER_ROLE_NATIVE_MESSAGES_ENV_VAR: Final[str] = "CODE_AGENT_OPENROUTER_ROLE_NATIVE_MESSAGES"


def _coerce_positive_int(value: object, *, default: int) -> int:
    """Parse a positive integer override or fall back to the default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if isinstance(value, float):
        try:
            parsed = int(value)
        except (OverflowError, ValueError):
            return default
        return parsed if parsed > 0 else default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            parsed = int(float(stripped))
        except (OverflowError, ValueError):
            return default
        return parsed if parsed > 0 else default
    return default


def _truncate_detail(text: str, *, max_characters: int = _DETAIL_PREVIEW_CHARACTERS) -> str:
    """Render bounded response details for adapter failures."""
    stripped = text.strip()
    if not stripped:
        return "<empty>"
    if len(stripped) <= max_characters:
        return stripped
    return f"{stripped[:max_characters]}...[truncated]"


def _coerce_bool(value: object, *, default: bool) -> bool:
    """Parse boolean-like values and fall back to the provided default."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _message_heading(message: CliRuntimeMessage, *, index: int) -> str:
    """Render a compact transcript heading for one runtime message."""
    if message.role == "tool":
        return f"### Message {index} ({message.role}:{message.tool_name})"
    return f"### Message {index} ({message.role})"


def _build_adapter_prompt(
    messages: Sequence[CliRuntimeMessage],
    *,
    system_prompt: str | None = None,
) -> str:
    """Build the prompt sent to OpenRouter for one runtime turn."""
    tool_guidance_lines = build_runtime_adapter_tool_guidance_lines(system_prompt=system_prompt)
    lines = [
        "You are the OpenRouter runtime adapter for a bounded coding worker.",
        (
            "Read the transcript below and return exactly one JSON object "
            "with no surrounding text, no markdown fences, and no explanation."
        ),
        "Choose one of two actions:",
        CliRuntimeStep(
            kind="tool_call",
            tool_name="<registered tool name>",
            tool_input="<tool input string>",
            final_output=None,
        ).model_dump_json(),
        CliRuntimeStep(
            kind="final",
            final_output="<final summary for the user>",
            tool_name=None,
            tool_input=None,
        ).model_dump_json(),
        "Rules:",
        "- Use only tool names listed in the system prompt's Available Tools section.",
        "- `tool_input` MUST be a string. If the tool expects JSON, encode that JSON as a string.",
        *tool_guidance_lines,
        "- If the transcript already contains enough information to finish, return `final`.",
        "- If the latest tool result failed, adapt to that failure instead of "
        "repeating blindly.",
        "- Return ONLY a raw JSON object. No markdown fences, no extra explanation.",
    ]
    if system_prompt is not None and system_prompt.strip():
        lines.extend(
            [
                "",
                "## Worker System Prompt",
                system_prompt.strip(),
            ]
        )
    lines.extend(
        [
            "",
            "## Runtime Transcript",
        ]
    )
    for index, message in enumerate(messages, start=1):
        lines.extend(
            (
                _message_heading(message, index=index),
                _message_content_to_text(message.content),
                "",
            )
        )
    return "\n".join(lines).rstrip()


def _build_role_native_instructions(*, system_prompt: str | None = None) -> str:
    """Build adapter protocol instructions for role-native chat payloads."""
    tool_guidance_lines = build_runtime_adapter_tool_guidance_lines(system_prompt=system_prompt)
    lines = [
        "You are the OpenRouter runtime adapter for a bounded coding worker.",
        (
            "Return exactly one JSON object with no surrounding text, "
            "no markdown fences, and no explanation."
        ),
        "Choose one of two actions.",
        "Examples:",
        "Example tool_call:",
        CliRuntimeStep(
            kind="tool_call",
            tool_name="<registered tool name>",
            tool_input="<tool input string>",
            final_output=None,
        ).model_dump_json(exclude_none=True),
        "Example final:",
        CliRuntimeStep(
            kind="final",
            final_output="<final summary for the user>",
            tool_name=None,
            tool_input=None,
        ).model_dump_json(exclude_none=True),
        "Rules:",
        "- Use only tool names listed in the system prompt's Available Tools section.",
        "- `tool_input` MUST be a string. If the tool expects JSON, encode that JSON as a string.",
        *tool_guidance_lines,
        "- If the transcript already contains enough information to finish, return `final`.",
        "- If the latest tool result failed, adapt to that failure instead of repeating blindly.",
        "- Return ONLY a raw JSON object. No markdown fences, no extra explanation.",
    ]
    if system_prompt is not None and system_prompt.strip():
        lines.extend(
            [
                "",
                "## Worker System Prompt",
                system_prompt.strip(),
            ]
        )
    return "\n".join(lines)


def _build_override_system_instructions(*, system_prompt: str | None = None) -> str:
    """Build override-safe system instructions that still enforce JSON-only output."""
    lines = [
        "You are the OpenRouter runtime adapter for a bounded coding worker.",
        (
            "Return exactly one JSON object with no surrounding text, "
            "no markdown fences, and no explanation."
        ),
        "Follow the user message instructions for the expected JSON schema and fields.",
    ]
    if system_prompt is not None and system_prompt.strip():
        lines.extend(
            [
                "",
                "## Worker System Prompt",
                system_prompt.strip(),
            ]
        )
    return "\n".join(lines)


def _build_role_native_request_messages(
    messages: Sequence[CliRuntimeMessage],
    *,
    system_prompt: str | None = None,
) -> list[ChatCompletionMessageParam]:
    """Build role-native request messages for OpenRouter-compatible chat APIs."""
    request_messages: list[ChatCompletionMessageParam] = [
        {
            "role": "system",
            "content": _build_role_native_instructions(system_prompt=system_prompt),
        }
    ]
    transcript = build_role_native_transcript(tuple(messages))
    request_messages.extend(
        cast(list[ChatCompletionMessageParam], serialize_openai_compatible_messages(transcript))
    )
    return request_messages


def _message_content_to_text(content: object) -> str:
    """Normalize chat completion message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _unwrap_markdown_json_fence(text: str) -> str:
    """Extract JSON payload from a fenced markdown block when present."""
    stripped = text.strip()
    if not stripped:
        return stripped
    fenced_matches = re.findall(
        r"```(?:json)?\s*(.*?)```",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced_matches:
        # Prefer the final fenced block since models may emit examples first.
        return fenced_matches[-1].strip()
    return stripped


class OpenRouterCliRuntimeAdapter(CliRuntimeAdapter):
    """Resolve runtime turns via OpenRouter's OpenAI-compatible chat API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_OPENROUTER_MODEL,
        base_url: str = DEFAULT_OPENROUTER_BASE_URL,
        request_timeout_seconds: int = DEFAULT_OPENROUTER_REQUEST_TIMEOUT_SECONDS,
        http_referer: str = DEFAULT_OPENROUTER_HTTP_REFERER,
        x_title: str = DEFAULT_OPENROUTER_X_TITLE,
        use_role_native_messages: bool = False,
    ) -> None:
        api_key_stripped = api_key.strip()
        if not api_key_stripped:
            raise ValueError("OpenRouter API key must be a non-empty string.")

        self.api_key = api_key_stripped
        self.model = model.strip() if model.strip() else DEFAULT_OPENROUTER_MODEL
        self.base_url = base_url
        self.request_timeout_seconds = _coerce_positive_int(
            request_timeout_seconds,
            default=DEFAULT_OPENROUTER_REQUEST_TIMEOUT_SECONDS,
        )
        self.http_referer = (
            http_referer.strip() if http_referer.strip() else DEFAULT_OPENROUTER_HTTP_REFERER
        )
        self.x_title = x_title.strip() if x_title.strip() else DEFAULT_OPENROUTER_X_TITLE
        self.use_role_native_messages = use_role_native_messages
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.request_timeout_seconds,
            default_headers={
                "HTTP-Referer": self.http_referer,
                "X-Title": self.x_title,
            },
        )

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> OpenRouterCliRuntimeAdapter:
        """Build an adapter from environment variables."""
        resolved_env = os.environ if environ is None else environ
        api_key = resolved_env.get(OPENROUTER_API_KEY_ENV_VAR, "")
        if not api_key.strip():
            raise RuntimeError("OpenRouter worker requires OPENROUTER_API_KEY to be configured.")

        return cls(
            api_key=api_key,
            model=resolved_env.get(OPENROUTER_MODEL_ENV_VAR, DEFAULT_OPENROUTER_MODEL),
            request_timeout_seconds=_coerce_positive_int(
                resolved_env.get(OPENROUTER_TIMEOUT_ENV_VAR),
                default=DEFAULT_OPENROUTER_REQUEST_TIMEOUT_SECONDS,
            ),
            http_referer=resolved_env.get(
                OPENROUTER_HTTP_REFERER_ENV_VAR,
                DEFAULT_OPENROUTER_HTTP_REFERER,
            ),
            x_title=resolved_env.get(OPENROUTER_X_TITLE_ENV_VAR, DEFAULT_OPENROUTER_X_TITLE),
            use_role_native_messages=_coerce_bool(
                resolved_env.get(OPENROUTER_ROLE_NATIVE_MESSAGES_ENV_VAR),
                default=False,
            ),
        )

    def next_step(
        self,
        messages: Sequence[CliRuntimeMessage],
        *,
        system_prompt: str | None = None,
        prompt_override: str | None = None,
        working_directory: Path | None = None,  # noqa: ARG002 - kept for interface symmetry
    ) -> CliRuntimeStep:
        """Ask OpenRouter for the next runtime step."""
        override_prompt = (
            prompt_override.strip() if prompt_override and prompt_override.strip() else None
        )
        request_messages: list[ChatCompletionMessageParam]
        if override_prompt is not None:
            request_messages = cast(
                list[ChatCompletionMessageParam],
                [
                    {
                        "role": "system",
                        "content": _build_override_system_instructions(system_prompt=system_prompt),
                    },
                    {"role": "user", "content": override_prompt},
                ],
            )
        elif self.use_role_native_messages:
            request_messages = _build_role_native_request_messages(
                messages,
                system_prompt=system_prompt,
            )
        else:
            request_messages = cast(
                list[ChatCompletionMessageParam],
                [
                    {
                        "role": "user",
                        "content": _build_adapter_prompt(messages, system_prompt=system_prompt),
                    }
                ],
            )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=request_messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # pragma: no cover - exercised via unit tests with stubs
            raise RuntimeError(f"OpenRouter adapter request failed: {exc}") from exc

        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError("OpenRouter adapter returned no choices.")

        first_choice = choices[0]
        if getattr(first_choice, "finish_reason", None) == "length":
            raise RuntimeError("OpenRouter response truncated due to token limit.")

        raw_json = _message_content_to_text(getattr(first_choice.message, "content", "")).strip()
        raw_json = _unwrap_markdown_json_fence(raw_json)
        if not raw_json:
            raise RuntimeError("OpenRouter adapter returned an empty response body.")
        if override_prompt is not None:
            try:
                return CliRuntimeStep.model_validate_json(raw_json)
            except Exception:
                return CliRuntimeStep(
                    kind="final",
                    final_output=raw_json,
                    tool_name=None,
                    tool_input=None,
                )
        try:
            return CliRuntimeStep.model_validate_json(raw_json)
        except Exception as exc:
            # Self-review prompts may return a different JSON schema (ReviewResult).
            # When that happens, preserve the payload by wrapping it as a final step.
            try:
                parsed_payload = json.loads(raw_json)
            except Exception:
                parsed_payload = None
            if isinstance(parsed_payload, dict):
                return CliRuntimeStep(
                    kind="final",
                    final_output=raw_json,
                    tool_name=None,
                    tool_input=None,
                )
            raise RuntimeError(
                "OpenRouter adapter returned a response that did not match "
                f"CliRuntimeStep: {_truncate_detail(raw_json)}"
            ) from exc
