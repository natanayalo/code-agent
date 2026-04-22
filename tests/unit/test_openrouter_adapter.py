"""Unit tests for the OpenRouter runtime adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from workers.cli_runtime import CliRuntimeMessage
from workers.openrouter_adapter import OpenRouterCliRuntimeAdapter


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
