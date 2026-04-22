"""Unit tests for the OpenRouter runtime adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from workers.cli_runtime import CliRuntimeMessage
from workers.openrouter_adapter import (
    OpenRouterCliRuntimeAdapter,
    _build_adapter_prompt,
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
        }
    )

    assert adapter.api_key == "key-123"
    assert adapter.model == "meta-llama/llama-3.1-70b-instruct"
    assert adapter.request_timeout_seconds == 45
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
