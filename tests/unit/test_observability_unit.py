"""Unit tests for observability tracing precedence and initialization suppression."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apps.observability import configure_tracing_from_env, is_tracing_enabled


def test_is_tracing_enabled_precedence() -> None:
    """Verify that is_tracing_enabled prioritizes explicit disable flags."""
    # Explicitly disabled
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": "0"}) is False
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": "false"}) is False
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": "no"}) is False
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": "off"}) is False

    # Explicitly enabled
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": "1"}) is True
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": "true"}) is True
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": "yes"}) is True
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": "on"}) is True

    # Missing or empty
    assert is_tracing_enabled({}) is False
    assert is_tracing_enabled({"CODE_AGENT_ENABLE_TRACING": ""}) is False


@patch("apps.observability._load_tracing_dependencies")
def test_bootstrap_tracing_skipped_when_disabled(mock_load_deps) -> None:
    """Verify that tracing bootstrap is completely skipped when disabled."""
    mock_deps = MagicMock()
    mock_load_deps.return_value = mock_deps

    # Set up disabled environment
    env = {"CODE_AGENT_ENABLE_TRACING": "0"}

    result = configure_tracing_from_env(service_name="test-service", environ=env)

    assert result.enabled is False
    assert result.reason == "disabled"
    # Registration should NEVER be called
    mock_deps.register_fn.assert_not_called()


@patch("apps.observability._load_tracing_dependencies")
def test_bootstrap_tracing_called_when_enabled(mock_load_deps) -> None:
    """Verify that tracing bootstrap proceeds when enabled."""
    import apps.observability

    apps.observability._bootstrap_complete = False

    mock_deps = MagicMock()
    mock_load_deps.return_value = mock_deps

    # Set up enabled environment
    env = {
        "CODE_AGENT_ENABLE_TRACING": "1",
        "CODE_AGENT_TRACING_OTLP_ENDPOINT": "http://localhost:6006",
    }

    result = configure_tracing_from_env(service_name="test-service", environ=env)

    assert result.enabled is True
    assert result.reason == "configured"
    mock_deps.register_fn.assert_called_once()
