"""Secret redaction for sandbox outputs."""

from __future__ import annotations

import re
from typing import Final

REDACTED_OUTPUT_LIMIT: Final[int] = 32768


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


def mask_url_credentials(text: str) -> str:
    """Mask credentials in repository URLs to prevent leaking secrets."""
    return re.sub(r"://[^/ ]+@", "://****@", text)


def sanitize_command(command: str, redactor: SecretRedactor | None) -> str:
    """Redact secrets from a command string for safe logging and tracing."""
    sanitized = mask_url_credentials(command)
    if not redactor:
        return sanitized
    return redactor.redact(sanitized)


def redact_and_truncate_output(
    text: str,
    redactor: SecretRedactor | None = None,
    limit_chars: int = REDACTED_OUTPUT_LIMIT,
) -> str:
    """Redact secrets and truncate text for safe logging and tracing."""
    if not text:
        return ""
    sanitized = mask_url_credentials(text)
    if redactor:
        sanitized = redactor.redact(sanitized)

    if len(sanitized) > limit_chars:
        return (
            sanitized[:limit_chars] + f"\n\n[TRUNCATED: Output exceeded {limit_chars} characters]"
        )
    return sanitized


def construct_sandbox_output(
    stdout: str,
    stderr: str,
    redactor: SecretRedactor | None = None,
    limit_chars: int = REDACTED_OUTPUT_LIMIT,
) -> str:
    """Construct a redacted summary of sandbox command output."""
    out = stdout or ""
    err = stderr or ""

    if not out and not err:
        return ""

    if not err:
        result = out
    elif not out:
        result = f"--- stderr ---\n{err}"
    else:
        result = f"{out}\n--- stderr ---\n{err}"

    return redact_and_truncate_output(result, redactor=redactor, limit_chars=limit_chars)
