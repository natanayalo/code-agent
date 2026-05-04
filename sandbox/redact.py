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


def sanitize_command(command: str, redactor: SecretRedactor | None) -> str:
    """Redact secrets from a command string for safe logging and tracing."""
    if not redactor:
        return command
    return redactor.redact(command)


def construct_sandbox_output(
    stdout: str, stderr: str, redactor: SecretRedactor | None = None
) -> str:
    """Construct a redacted summary of sandbox command output."""
    out = stdout or ""
    err = stderr or ""
    if redactor:
        out = redactor.redact(out)
        err = redactor.redact(err)

    if not out and not err:
        return ""
    if not err:
        return out
    if not out:
        return f"--- stderr ---\n{err}"
    return f"{out}\n--- stderr ---\n{err}"
