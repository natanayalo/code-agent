"""Immutable broker-owned provider host lists for network egress capability enforcement."""

from __future__ import annotations

CODEX_API_KEY_HOSTS: tuple[str, ...] = ("api.openai.com",)

CODEX_CHATGPT_HOSTS: tuple[str, ...] = (
    "chatgpt.com",
    "auth.openai.com",
)

GEMINI_API_KEY_HOSTS: tuple[str, ...] = ("generativelanguage.googleapis.com",)

GEMINI_OAUTH_HOSTS: tuple[str, ...] = (
    "cloudcode-pa.googleapis.com",
    "oauth2.googleapis.com",
)

# For backwards compatibility with places that might import it:
CODEX_RUNTIME_HOSTS = CODEX_API_KEY_HOSTS
GEMINI_RUNTIME_HOSTS = GEMINI_API_KEY_HOSTS
