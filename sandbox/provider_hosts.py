"""Immutable broker-owned provider host lists for network egress capability enforcement."""

from __future__ import annotations

CODEX_RUNTIME_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "auth.openai.com",
)

GEMINI_RUNTIME_HOSTS: tuple[str, ...] = (
    "generativelanguage.googleapis.com",
    "oauth2.googleapis.com",
)
