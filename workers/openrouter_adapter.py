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
from workers.adapter_parsing import final_cli_runtime_step, parse_cli_runtime_step_or_final
from workers.adapter_prompts import (
    DEFAULT_RAW_JSON_ONLY_RULE,
    OPENROUTER_ADAPTER_IDENTITY_LINE,
    build_final_example_json,
    build_override_system_instructions,
    build_role_native_system_instructions,
    build_runtime_transcript_prompt,
    build_tool_call_example_json,
    json_only_response_line,
)
from workers.adapter_utils import (
    coerce_bool,
    coerce_positive_int,
    normalize_prompt_override,
    truncate_detail_keep_head,
)
from workers.cli_runtime import CliRuntimeAdapter, CliRuntimeMessage, CliRuntimeStep
from workers.llm_tracing import set_llm_span_output, with_llm_span
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
TRACER_NAME: Final[str] = "workers.openrouter"


def _build_adapter_prompt(
    messages: Sequence[CliRuntimeMessage],
    *,
    system_prompt: str | None = None,
) -> str:
    """Build the prompt sent to OpenRouter for one runtime turn."""
    tool_guidance_lines = build_runtime_adapter_tool_guidance_lines(system_prompt=system_prompt)
    return build_runtime_transcript_prompt(
        identity_line=OPENROUTER_ADAPTER_IDENTITY_LINE,
        response_instruction_line=json_only_response_line(include_transcript_reference=True),
        tool_call_example=build_tool_call_example_json(),
        final_example=build_final_example_json(),
        tool_guidance_lines=tool_guidance_lines,
        messages=messages,
        system_prompt=system_prompt,
        json_output_rule=DEFAULT_RAW_JSON_ONLY_RULE,
        content_transform=_message_content_to_text,
    )


def _build_role_native_instructions(*, system_prompt: str | None = None) -> str:
    """Build adapter protocol instructions for role-native chat payloads."""
    tool_guidance_lines = build_runtime_adapter_tool_guidance_lines(system_prompt=system_prompt)
    return build_role_native_system_instructions(
        identity_line=OPENROUTER_ADAPTER_IDENTITY_LINE,
        json_only_response_line=json_only_response_line(include_transcript_reference=False),
        tool_call_example=build_tool_call_example_json(exclude_none=True),
        final_example=build_final_example_json(exclude_none=True),
        tool_guidance_lines=tool_guidance_lines,
        system_prompt=system_prompt,
        json_output_rule=DEFAULT_RAW_JSON_ONLY_RULE,
    )


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
        self.request_timeout_seconds = coerce_positive_int(
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
            request_timeout_seconds=coerce_positive_int(
                resolved_env.get(OPENROUTER_TIMEOUT_ENV_VAR),
                default=DEFAULT_OPENROUTER_REQUEST_TIMEOUT_SECONDS,
            ),
            http_referer=resolved_env.get(
                OPENROUTER_HTTP_REFERER_ENV_VAR,
                DEFAULT_OPENROUTER_HTTP_REFERER,
            ),
            x_title=resolved_env.get(OPENROUTER_X_TITLE_ENV_VAR, DEFAULT_OPENROUTER_X_TITLE),
            use_role_native_messages=coerce_bool(
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
        override_prompt = normalize_prompt_override(prompt_override)
        request_messages: list[ChatCompletionMessageParam]
        if override_prompt is not None:
            if self.use_role_native_messages:
                request_messages = cast(
                    list[ChatCompletionMessageParam],
                    [
                        {
                            "role": "system",
                            "content": build_override_system_instructions(
                                identity_line=OPENROUTER_ADAPTER_IDENTITY_LINE,
                                json_only_response_line=json_only_response_line(
                                    include_transcript_reference=False
                                ),
                                follow_user_rule=(
                                    "Follow the user message instructions for the expected JSON "
                                    "schema and fields."
                                ),
                                system_prompt=system_prompt,
                            ),
                        },
                        {"role": "user", "content": override_prompt},
                    ],
                )
            else:
                request_messages = cast(
                    list[ChatCompletionMessageParam],
                    [{"role": "user", "content": override_prompt}],
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
            with with_llm_span(
                tracer_name=TRACER_NAME,
                span_name="openrouter.chat",
                input_data=request_messages,
            ):
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=request_messages,
                    response_format={"type": "json_object"},
                )
                from typing import Any

                output_val: Any = (
                    response.model_dump() if hasattr(response, "model_dump") else response
                )
                try:
                    if response.choices and response.choices[0].message.content:
                        try:
                            output_val = json.loads(response.choices[0].message.content)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            output_val = response.choices[0].message.content
                except (AttributeError, IndexError):
                    pass
                set_llm_span_output(output_val)
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
            return parse_cli_runtime_step_or_final(raw_json)
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
                return final_cli_runtime_step(raw_json)
            raise RuntimeError(
                "OpenRouter adapter returned a response that did not match "
                "CliRuntimeStep: "
                f"{truncate_detail_keep_head(raw_json, max_characters=_DETAIL_PREVIEW_CHARACTERS)}"
            ) from exc
