"""Unit tests for API auth config helpers."""

from __future__ import annotations

from apps.api.auth import build_api_auth_config_from_env


def test_build_api_auth_config_from_env_trims_secrets() -> None:
    """Configured secrets should be normalized by stripping whitespace."""
    config = build_api_auth_config_from_env(
        {
            "CODE_AGENT_API_SHARED_SECRET": "  shared-secret  ",
            "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": "  telegram-secret  ",
        }
    )

    assert config.shared_secret == "shared-secret"
    assert config.telegram_webhook_secret == "telegram-secret"


def test_build_api_auth_config_from_env_treats_blank_as_missing() -> None:
    """Blank secrets should be treated as unset values."""
    config = build_api_auth_config_from_env(
        {
            "CODE_AGENT_API_SHARED_SECRET": "   ",
            "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN": "\t",
        }
    )

    assert config.shared_secret is None
    assert config.telegram_webhook_secret is None
