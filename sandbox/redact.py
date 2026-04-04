"""Secret redaction for sandbox outputs."""

from __future__ import annotations

import re


class SecretRedactor:
    """Mask sensitive strings in text."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        """Initialize the redactor with a list of secrets to mask."""
        # Filter out empty or whitespace-only secrets
        clean_secrets = {s for s in (secrets or []) if s and s.strip()}
        # Sort secrets by length descending to avoid partial masking of longer secrets
        # that contain shorter secrets (e.g. "password123" vs "password").
        self._secrets = sorted(clean_secrets, key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(re.escape(s) for s in self._secrets)) if self._secrets else None
        )

    def redact(self, text: str) -> str:
        """Replace all known secrets in text with [REDACTED]."""
        if not self._pattern or not text:
            return text
        return self._pattern.sub("[REDACTED]", text)
