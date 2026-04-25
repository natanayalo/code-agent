"""Unit tests for the OpenRouter runtime adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from workers.cli_runtime import CliRuntimeMessage
from workers.openrouter_adapter import (
    OpenRouterCliRuntimeAdapter,
    _build_adapter_prompt,
    _build_role_native_instructions,
    _build_role_native_request_messages,
    _coerce_bool,
    _message_content_to_text,
)


class _FakeOpenAI:
    """Test double capturing OpenAI client initialization and chat requests."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[dict[str, object]] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"kind":"final","tool_name":null,"tool_input":null,"final_output":"done"}'
                    )
                )
            ]
        )


def test_openrouter_adapter_from_env_requires_api_key() -> None:
    """OpenRouter adapter should fail fast when API key is missing."""
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterCliRuntimeAdapter.from_env({})


def test_openrouter_adapter_from_env_maps_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables should map into adapter settings and headers."""

    fake_client: _FakeOpenAI | None = None

    def _fake_openai(**kwargs):
        nonlocal fake_client
        fake_client = _FakeOpenAI(**kwargs)
        return fake_client

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)

    adapter = OpenRouterCliRuntimeAdapter.from_env(
        {
            "OPENROUTER_API_KEY": " key-123 ",
            "CODE_AGENT_OPENROUTER_MODEL": "meta-llama/llama-3.1-70b-instruct",
            "CODE_AGENT_OPENROUTER_TIMEOUT_SECONDS": "45",
            "CODE_AGENT_OPENROUTER_HTTP_REFERER": "https://example.com/app",
            "CODE_AGENT_OPENROUTER_X_TITLE": "Code Agent Dev",
            "CODE_AGENT_OPENROUTER_ROLE_NATIVE_MESSAGES": "true",
        }
    )

    assert adapter.api_key == "key-123"
    assert adapter.model == "meta-llama/llama-3.1-70b-instruct"
    assert adapter.request_timeout_seconds == 45
    assert adapter.use_role_native_messages is True
    assert fake_client is not None
    assert fake_client.kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert fake_client.kwargs["default_headers"] == {
        "HTTP-Referer": "https://example.com/app",
        "X-Title": "Code Agent Dev",
    }


def test_openrouter_adapter_next_step_requests_chat_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter should call OpenRouter and parse the returned step JSON."""

    fake_client: _FakeOpenAI | None = None

    def _fake_openai(**kwargs):
        nonlocal fake_client
        fake_client = _FakeOpenAI(**kwargs)
        return fake_client

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)

    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")

    step = adapter.next_step([CliRuntimeMessage(role="system", content="Proceed")])

    assert step.kind == "final"
    assert step.final_output == "done"
    assert fake_client is not None
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["model"] == "anthropic/claude-3.5-sonnet"
    assert fake_client.calls[0]["response_format"] == {"type": "json_object"}


def test_openrouter_adapter_next_step_role_native_mode_uses_structured_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role-native mode should send a structured message array to OpenRouter."""

    fake_client: _FakeOpenAI | None = None

    def _fake_openai(**kwargs):
        nonlocal fake_client
        fake_client = _FakeOpenAI(**kwargs)
        return fake_client

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)
    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key", use_role_native_messages=True)

    step = adapter.next_step(
        [
            CliRuntimeMessage(role="system", content="Runtime system context."),
            CliRuntimeMessage(role="assistant", content='{"kind":"tool_call"}'),
            CliRuntimeMessage(
                role="tool",
                tool_name="execute_bash",
                content="Exit code: 0\nOutput:\nhello",
            ),
        ],
        system_prompt="Worker policy text.",
    )

    assert step.kind == "final"
    assert fake_client is not None
    payload_messages = fake_client.calls[0]["messages"]
    assert isinstance(payload_messages, list)
    assert payload_messages[0]["role"] == "system"
    assert "Return exactly one JSON object" in payload_messages[0]["content"]
    assert "## Worker System Prompt\nWorker policy text." in payload_messages[0]["content"]
    assert payload_messages[1] == {"role": "system", "content": "Runtime system context."}
    assert payload_messages[2] == {"role": "assistant", "content": '{"kind":"tool_call"}'}
    assert payload_messages[3] == {
        "role": "user",
        "content": "Tool result (execute_bash):\nExit code: 0\nOutput:\nhello",
    }


def test_openrouter_adapter_next_step_rejects_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter should raise a clear error when OpenRouter returns no content."""

    class _EmptyResponseOpenAI(_FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])

    def _fake_openai(**kwargs):
        return _EmptyResponseOpenAI(**kwargs)

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)

    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")

    with pytest.raises(RuntimeError, match="empty response body"):
        adapter.next_step([CliRuntimeMessage(role="system", content="Proceed")])


def test_openrouter_adapter_next_step_accepts_markdown_fenced_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter should tolerate markdown-fenced JSON payloads from providers."""

    class _FencedResponseOpenAI(_FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                "```json\n"
                                '{"kind":"final","tool_name":null,"tool_input":null,"final_output":"done"}\n'
                                "```"
                            )
                        )
                    )
                ]
            )

    def _fake_openai(**kwargs):
        return _FencedResponseOpenAI(**kwargs)

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)
    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")
    step = adapter.next_step([CliRuntimeMessage(role="system", content="Proceed")])
    assert step.kind == "final"
    assert step.final_output == "done"


def test_openrouter_adapter_next_step_accepts_preamble_before_fenced_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter should extract fenced JSON even when preamble text is present."""

    class _PreambleFencedResponseOpenAI(_FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                "Here is the JSON response:\n\n"
                                "```json\n"
                                '{"kind":"final","tool_name":null,"tool_input":null,"final_output":"done"}\n'
                                "```"
                            )
                        )
                    )
                ]
            )

    def _fake_openai(**kwargs):
        return _PreambleFencedResponseOpenAI(**kwargs)

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)
    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")
    step = adapter.next_step([CliRuntimeMessage(role="system", content="Proceed")])
    assert step.kind == "final"
    assert step.final_output == "done"


def test_openrouter_adapter_next_step_prefers_last_fenced_json_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When multiple fenced blocks exist, adapter should parse the final one."""

    class _MultipleFencedResponseOpenAI(_FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                'Example:\n```json\n{"kind":"final","final_output":"wrong",'
                                '"tool_name":null,"tool_input":null}\n```\n'
                                'Actual:\n```json\n{"kind":"final","final_output":"done",'
                                '"tool_name":null,"tool_input":null}\n```'
                            )
                        )
                    )
                ]
            )

    def _fake_openai(**kwargs):
        return _MultipleFencedResponseOpenAI(**kwargs)

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)
    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")
    step = adapter.next_step([CliRuntimeMessage(role="system", content="Proceed")])
    assert step.kind == "final"
    assert step.final_output == "done"


def test_openrouter_adapter_next_step_rejects_missing_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter should fail clearly when response has no choices."""

    class _NoChoicesOpenAI(_FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[])

    def _fake_openai(**kwargs):
        return _NoChoicesOpenAI(**kwargs)

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)
    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")
    with pytest.raises(RuntimeError, match="no choices"):
        adapter.next_step([CliRuntimeMessage(role="system", content="Proceed")])


def test_openrouter_adapter_next_step_rejects_truncated_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter should surface token-truncation before JSON validation."""

    class _TruncatedResponseOpenAI(_FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="length",
                        message=SimpleNamespace(
                            content='{"kind":"final","tool_name":null,"tool_input":null'
                        ),
                    )
                ]
            )

    def _fake_openai(**kwargs):
        return _TruncatedResponseOpenAI(**kwargs)

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)
    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")
    with pytest.raises(RuntimeError, match="truncated due to token limit"):
        adapter.next_step([CliRuntimeMessage(role="system", content="Proceed")])


def test_openrouter_adapter_next_step_wraps_non_runtime_json_as_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter should preserve valid non-step JSON by wrapping it as final output."""

    class _ReviewJsonResponseOpenAI(_FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"reviewer_kind":"worker_self_review","summary":"ok",'
                                '"confidence":0.8,"outcome":"no_findings","findings":[]}'
                            )
                        )
                    )
                ]
            )

    def _fake_openai(**kwargs):
        return _ReviewJsonResponseOpenAI(**kwargs)

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)
    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")
    step = adapter.next_step([CliRuntimeMessage(role="system", content="Proceed")])
    assert step.kind == "final"
    assert step.final_output is not None
    assert '"reviewer_kind":"worker_self_review"' in step.final_output


def test_openrouter_adapter_prompt_override_bypasses_runtime_prompt_shaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt overrides should include JSON-only system rules and preserve user prompt."""

    fake_client: _FakeOpenAI | None = None

    class _ReviewJsonResponseOpenAI(_FakeOpenAI):
        def _create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=(
                                '{"reviewer_kind":"worker_self_review","summary":"ok",'
                                '"confidence":0.8,"outcome":"no_findings","findings":[]}'
                            )
                        )
                    )
                ]
            )

    def _fake_openai(**kwargs):
        nonlocal fake_client
        fake_client = _ReviewJsonResponseOpenAI(**kwargs)
        return fake_client

    monkeypatch.setattr("workers.openrouter_adapter.OpenAI", _fake_openai)
    adapter = OpenRouterCliRuntimeAdapter(api_key="test-key")
    step = adapter.next_step(
        [],
        prompt_override="Review these edits and return ReviewResult JSON only.",
    )

    assert step.kind == "final"
    assert step.final_output is not None
    assert '"reviewer_kind":"worker_self_review"' in step.final_output
    assert fake_client is not None
    assert len(fake_client.calls) == 1
    assert "Return exactly one JSON object" in fake_client.calls[0]["messages"][0]["content"]
    assert (
        fake_client.calls[0]["messages"][1]["content"]
        == "Review these edits and return ReviewResult JSON only."
    )


def test_build_role_native_request_messages_serializes_tool_transcript_entries() -> None:
    """Tool transcript messages should serialize with explicit tool labels."""
    request_messages = _build_role_native_request_messages(
        (
            CliRuntimeMessage(role="system", content="Runtime instructions"),
            CliRuntimeMessage(role="assistant", content="tool call emitted"),
            CliRuntimeMessage(role="tool", tool_name="search_dir", content="2 matches"),
        ),
        system_prompt="Worker system prompt",
    )

    assert request_messages[0]["role"] == "system"
    assert "## Worker System Prompt\nWorker system prompt" in request_messages[0]["content"]
    assert request_messages[1] == {"role": "system", "content": "Runtime instructions"}
    assert request_messages[2] == {"role": "assistant", "content": "tool call emitted"}
    assert request_messages[3] == {
        "role": "user",
        "content": "Tool result (search_dir):\n2 matches",
    }


def test_coerce_bool_parses_supported_values() -> None:
    """Boolean parser should accept common env-style truthy/falsy strings."""
    assert _coerce_bool(True, default=False) is True
    assert _coerce_bool("true", default=False) is True
    assert _coerce_bool(" YES ", default=False) is True
    assert _coerce_bool("0", default=True) is False
    assert _coerce_bool("off", default=True) is False
    assert _coerce_bool("unknown", default=True) is True


def test_build_role_native_instructions_labels_json_examples() -> None:
    """Role-native instruction examples should be explicitly labeled."""
    instructions = _build_role_native_instructions()
    assert "Examples:" in instructions
    assert "Example tool_call:" in instructions
    assert "Example final:" in instructions


def test_message_content_to_text_joins_text_blocks() -> None:
    """Adapter content normalization should join text blocks from list content."""
    content = [
        {"type": "text", "text": "alpha"},
        {"type": "input_text", "text": "beta"},
        {"type": "image_url", "url": "https://example.com/img.png"},
    ]
    assert _message_content_to_text(content) == "alpha\nbeta"


def test_build_adapter_prompt_normalizes_non_string_content() -> None:
    """Prompt builder should normalize non-string content before joining lines."""
    message = CliRuntimeMessage.model_construct(
        role="system",
        content=[{"type": "text", "text": "structured"}],
        tool_name=None,
    )
    prompt = _build_adapter_prompt((message,))
    assert "structured" in prompt


def test_build_adapter_prompt_preserves_rules_with_worker_system_prompt() -> None:
    """Worker system prompt should augment, not replace, adapter protocol rules."""
    message = CliRuntimeMessage(role="system", content="Proceed")
    prompt = _build_adapter_prompt((message,), system_prompt="Reviewer instructions")

    assert "## Worker System Prompt" in prompt
    assert "Reviewer instructions" in prompt
    assert "Choose one of two actions:" in prompt
