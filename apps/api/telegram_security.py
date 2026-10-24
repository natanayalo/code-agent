"""Fail-closed configuration for the single-operator Telegram boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping

TELEGRAM_BODY_LIMIT = 256 * 1024
SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z", re.ASCII)


def parse_telegram_ids(value: str, *, setting: str) -> frozenset[int]:
    """Parse positive numeric private-chat/user IDs without echoing configuration."""
    if not value.strip():
        return frozenset()
    parts = [part.strip() for part in value.split(",")]
    if any(not re.fullmatch(r"[1-9][0-9]{0,15}", part, re.ASCII) for part in parts):
        raise ValueError(f"{setting} must contain comma-separated positive numeric IDs")
    return frozenset(int(part) for part in parts)


def telegram_enabled_from_env(env: Mapping[str, str]) -> bool:
    """Reject misspelled enablement instead of silently changing ingress policy."""
    value = env.get("CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED", "0").strip().lower()
    if value not in {"0", "false", "no", "off", "1", "true", "yes", "on"}:
        raise ValueError("CODE_AGENT_TELEGRAM_WEBHOOK_ENABLED must be a boolean")
    return value in {"1", "true", "yes", "on"}


def validate_telegram_configuration(
    *, enabled: bool, secret: str | None, users: frozenset[int], chats: frozenset[int]
) -> None:
    """Validate injected configuration as well as environment-loaded settings."""
    if not enabled:
        return
    if not secret or not SECRET_PATTERN.fullmatch(secret):
        raise ValueError("Enabled Telegram webhook requires a 32-256 character URL-safe secret")
    if (
        not users
        or not chats
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in users | chats
        )
    ):
        raise ValueError(
            "Enabled Telegram webhook requires numeric user and private-chat allowlists"
        )
