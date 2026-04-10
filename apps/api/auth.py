"""Authentication configuration and request guards for API routes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

API_SHARED_SECRET_ENV_VAR: Final[str] = "CODE_AGENT_API_SHARED_SECRET"
TELEGRAM_WEBHOOK_SECRET_ENV_VAR: Final[str] = "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN"
API_SHARED_SECRET_HEADER: Final[str] = "X-Webhook-Token"
TELEGRAM_WEBHOOK_SECRET_HEADER: Final[str] = "X-Telegram-Bot-Api-Secret-Token"


def _clean_secret(value: str | None) -> str | None:
    """Normalize optional secret values from environment variables."""
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


@dataclass(frozen=True, slots=True)
class ApiAuthConfig:
    """Authentication secrets configured for inbound API routes."""

    shared_secret: str | None = None
    telegram_webhook_secret: str | None = None


def build_api_auth_config_from_env(environ: Mapping[str, str] | None = None) -> ApiAuthConfig:
    """Load inbound API authentication settings from environment variables."""
    resolved_env = os.environ if environ is None else environ
    return ApiAuthConfig(
        shared_secret=_clean_secret(resolved_env.get(API_SHARED_SECRET_ENV_VAR)),
        telegram_webhook_secret=_clean_secret(resolved_env.get(TELEGRAM_WEBHOOK_SECRET_ENV_VAR)),
    )
