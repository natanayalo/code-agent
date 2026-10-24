"""Authentication configuration and request guards for API routes."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import jwt

from apps.api.telegram_security import (
    parse_telegram_ids,
    telegram_enabled_from_env,
    validate_telegram_configuration,
)


class RequestProto(Protocol):
    """Minimal protocol for objects with headers (like FastAPI Request)."""

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def url(self) -> Any: ...


logger = logging.getLogger(__name__)

API_SHARED_SECRET_ENV_VAR: Final[str] = "CODE_AGENT_API_SHARED_SECRET"
TELEGRAM_WEBHOOK_SECRET_ENV_VAR: Final[str] = "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN"
ALLOWED_ORIGINS_ENV_VAR: Final[str] = "CODE_AGENT_ALLOWED_ORIGINS"

# Security override constants
COOKIE_SECURE_ENV_VAR: Final[str] = "CODE_AGENT_COOKIE_SECURE"
API_FORCE_HTTPS_ENV_VAR: Final[str] = "CODE_AGENT_API_FORCE_HTTPS"

API_SHARED_SECRET_HEADER: Final[str] = "X-Webhook-Token"
TELEGRAM_WEBHOOK_SECRET_HEADER: Final[str] = "X-Telegram-Bot-Api-Secret-Token"
DASHBOARD_COOKIE_NAME: Final[str] = "agent_session"

JWT_ALGORITHM: Final[str] = "HS256"
JWT_EXPIRY_SECONDS: Final[int] = 3600  # 1 hour


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
    allowed_origins: list[str] = field(default_factory=list)
    cookie_secure_override: bool | None = None
    force_https: bool = False
    telegram_webhook_enabled: bool = False
    telegram_allowed_user_ids: frozenset[int] = frozenset()
    telegram_allowed_chat_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        validate_telegram_configuration(
            enabled=self.telegram_webhook_enabled,
            secret=self.telegram_webhook_secret,
            users=self.telegram_allowed_user_ids,
            chats=self.telegram_allowed_chat_ids,
        )

    def is_cookie_secure(self, request: RequestProto | None = None) -> bool:
        """Determine if cookies should be marked Secure based on config and request."""
        # 1. Explicit override via CODE_AGENT_COOKIE_SECURE always wins
        if self.cookie_secure_override is not None:
            return self.cookie_secure_override

        # 2. Force HTTPS override (resolved at load time)
        if self.force_https:
            return True

        # 3. Pragmatic default: trust request scheme or X-Forwarded-Proto
        if request:
            # Check direct scheme first
            url = getattr(request, "url", None)
            if url and getattr(url, "scheme", None) == "https":
                return True

            # Trust X-Forwarded-Proto case-insensitively if present
            # Handle comma-separated values (common in multi-proxy setups)
            headers = getattr(request, "headers", {})
            proto = next(
                (value for key, value in headers.items() if key.lower() == "x-forwarded-proto"),
                "",
            )
            if any(p.strip().lower() == "https" for p in proto.split(",")):
                return True

        return False


def build_api_auth_config_from_env(environ: Mapping[str, str] | None = None) -> ApiAuthConfig:
    """Load inbound API authentication settings from environment variables."""
    env = environ if environ is not None else os.environ

    allowed_origins_str = env.get(ALLOWED_ORIGINS_ENV_VAR, "")
    allowed_origins = [
        o.strip().rstrip("/").lower() for o in allowed_origins_str.split(",") if o.strip()
    ]

    # Resolve overrides
    cookie_secure_val = env.get(COOKIE_SECURE_ENV_VAR)
    cookie_secure_override = None
    if cookie_secure_val is not None:
        cookie_secure_override = cookie_secure_val.lower() in ("true", "1")

    force_https = env.get(API_FORCE_HTTPS_ENV_VAR, "").lower() in ("true", "1")
    telegram_enabled = telegram_enabled_from_env(env)

    return ApiAuthConfig(
        shared_secret=_clean_secret(env.get(API_SHARED_SECRET_ENV_VAR)),
        telegram_webhook_secret=(
            env.get(TELEGRAM_WEBHOOK_SECRET_ENV_VAR)
            if telegram_enabled
            else _clean_secret(env.get(TELEGRAM_WEBHOOK_SECRET_ENV_VAR))
        ),
        allowed_origins=allowed_origins,
        cookie_secure_override=cookie_secure_override,
        force_https=force_https,
        telegram_webhook_enabled=telegram_enabled,
        telegram_allowed_user_ids=parse_telegram_ids(
            env.get("CODE_AGENT_TELEGRAM_ALLOWED_USER_IDS", ""),
            setting="CODE_AGENT_TELEGRAM_ALLOWED_USER_IDS",
        ),
        telegram_allowed_chat_ids=parse_telegram_ids(
            env.get("CODE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS", ""),
            setting="CODE_AGENT_TELEGRAM_ALLOWED_CHAT_IDS",
        ),
    )


def create_dashboard_token(secret: str) -> str:
    """Create a signed JWT for the dashboard session."""
    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
        "sub": "operator",
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_dashboard_token(token: str, secret: str) -> dict[str, Any] | None:
    """Decode and validate a dashboard session token."""
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
        return payload if payload.get("sub") == "operator" else None
    except jwt.PyJWTError:
        return None
