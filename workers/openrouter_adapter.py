"""Concrete OpenRouter adapter using the OpenAI-compatible API client."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from openai import OpenAI

from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep

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
    return f"[truncated]...{stripped[-max_characters:].lstrip()}"


def _message_heading(message: CliRuntimeMessage, *, index: int) -> str:
    """Render a compact transcript heading for one runtime message."""
    if message.role == "tool":
        return f"### Message {index} ({message.role}:{message.tool_name})"
    return f"### Message {index} ({message.role})"


def _build_adapter_prompt(messages: Sequence[CliRuntimeMessage]) -> str:
    """Build the prompt sent to OpenRouter for one runtime turn."""
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
        "- For `execute_bash`, return one focused shell command as the tool_input string.",
        (
            "- For `execute_git`, return the tool_input as a compact JSON object encoded "
            'as a string, for example {"operation":"status","porcelain":true}.'
        ),
        (
            "- For `execute_github`, return the tool_input as a compact JSON object encoded "
            "as a string, for example "
            '{"operation":"pr_comment","repository_full_name":"owner/repo",'
            '"pr_number":1,"comment_body":"Looks good."}.'
        ),
        (
            "- For `execute_browser`, return the tool_input as a compact JSON object encoded "
            "as a string, for example "
            '{"operation":"search","query":"langgraph","limit":3}.'
        ),
        "- If the transcript already contains enough information to finish, return `final`.",
        "- If the latest tool result failed, adapt to that failure instead of repeating blindly.",
        "- Return ONLY a raw JSON object. No markdown fences, no extra explanation.",
        "",
        "## Runtime Transcript",
    ]
    for index, message in enumerate(messages, start=1):
        lines.extend((_message_heading(message, index=index), message.content, ""))
    return "\n".join(lines).rstrip()


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
        )

    def next_step(
        self,
        messages: Sequence[CliRuntimeMessage],
        *,
        working_directory: Path | None = None,  # noqa: ARG002 - kept for interface symmetry
    ) -> CliRuntimeStep:
        """Ask OpenRouter for the next runtime step."""
        prompt = _build_adapter_prompt(messages)

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # pragma: no cover - exercised via unit tests with stubs
            raise RuntimeError(f"OpenRouter adapter request failed: {exc}") from exc

        try:
            first_choice = response.choices[0]
        except Exception as exc:
            raise RuntimeError("OpenRouter adapter returned no choices.") from exc

        raw_json = _message_content_to_text(getattr(first_choice.message, "content", "")).strip()
        if not raw_json:
            raise RuntimeError("OpenRouter adapter returned an empty response body.")
        try:
            return CliRuntimeStep.model_validate_json(raw_json)
        except Exception as exc:
            raise RuntimeError(
                "OpenRouter adapter returned a response that did not match "
                f"CliRuntimeStep: {_truncate_detail(raw_json)}"
            ) from exc
