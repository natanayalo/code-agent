"""Authentication configuration and request guards for API routes."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import jwt


class RequestProto(Protocol):
    """Minimal protocol for objects with headers (like FastAPI Request)."""

    headers: Mapping[str, str]


logger = logging.getLogger(__name__)

API_SHARED_SECRET_ENV_VAR: Final[str] = "CODE_AGENT_API_SHARED_SECRET"
TELEGRAM_WEBHOOK_SECRET_ENV_VAR: Final[str] = "CODE_AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN"
ALLOWED_ORIGINS_ENV_VAR: Final[str] = "CODE_AGENT_ALLOWED_ORIGINS"
COOKIE_SECURE_ENV_VAR: Final[str] = "CODE_AGENT_COOKIE_SECURE"

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
    cookie_secure: bool = False

    def is_cookie_secure(self, request: RequestProto | None = None) -> bool:
        """Determine if cookies should be marked Secure based on config and request."""
        # Explicit override always wins
        if os.environ.get(COOKIE_SECURE_ENV_VAR) is not None:
            return self.cookie_secure

        # Pragmatic default: trust X-Forwarded-Proto if present
        if request and request.headers.get("X-Forwarded-Proto") == "https":
            return True

        return self.cookie_secure


def build_api_auth_config_from_env(environ: Mapping[str, str] | None = None) -> ApiAuthConfig:
    """Load inbound API authentication settings from environment variables."""
    resolved_env = os.environ if environ is None else environ

    allowed_origins_str = resolved_env.get(ALLOWED_ORIGINS_ENV_VAR, "")
    allowed_origins = [
        o.strip().rstrip("/").lower() for o in allowed_origins_str.split(",") if o.strip()
    ]

    cookie_secure = resolved_env.get(COOKIE_SECURE_ENV_VAR, "0") == "1"

    return ApiAuthConfig(
        shared_secret=_clean_secret(resolved_env.get(API_SHARED_SECRET_ENV_VAR)),
        telegram_webhook_secret=_clean_secret(resolved_env.get(TELEGRAM_WEBHOOK_SECRET_ENV_VAR)),
        allowed_origins=allowed_origins,
        cookie_secure=cookie_secure,
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
