"""Shared low-level utility helpers for runtime adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.observability import NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH

if TYPE_CHECKING:
    from workers.native_agent_models import NativeAgentRunResult


def coerce_positive_int(value: object, *, default: int) -> int:
    """Parse a positive integer override or fall back to the default."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if isinstance(value, float):
        try:
            parsed = int(value)
        except (OverflowError, ValueError):
            return default
        return parsed if parsed > 0 else default
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            parsed = int(float(stripped))
        except (OverflowError, ValueError):
            return default
        return parsed if parsed > 0 else default
    return default


def coerce_bool(value: object, *, default: bool) -> bool:
    """Parse boolean-like values and fall back to the provided default."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def normalize_prompt_override(prompt_override: str | None) -> str | None:
    """Normalize optional prompt overrides, returning None for blank input."""
    if prompt_override is None:
        return None
    normalized = prompt_override.strip()
    return normalized if normalized else None


def truncate_detail_keep_tail(text: str, *, max_characters: int) -> str:
    """Render bounded text keeping the trailing suffix for context."""
    if not text:
        return "<empty>"

    # Avoid copying the entire string if it exceeds the limit.
    if len(text) <= max_characters:
        stripped = text.strip()
        return stripped if stripped else "<empty>"

    # Extract the tail first, then strip. This prevents a full copy of a large input string.
    tail = text[-max_characters:].lstrip()
    return f"[truncated]...{tail}" if tail else "[truncated]..."


def truncate_detail_keep_head(text: str, *, max_characters: int) -> str:
    """Render bounded text keeping the leading prefix for context."""
    if not text:
        return "<empty>"

    if len(text) <= max_characters:
        stripped = text.strip()
        return stripped if stripped else "<empty>"

    # Extract the head first, then strip. This prevents a full copy of a large input string.
    head = text[:max_characters].rstrip()
    return f"{head}...[truncated]" if head else "...[truncated]"


def format_native_run_summary(
    result: NativeAgentRunResult, *, max_characters: int | None = None
) -> str:
    """Format a human-readable summary from a native agent run result."""
    limit = max_characters or NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH
    base = result.final_message or result.summary
    if result.status == "success":
        return base

    # Include truncated stderr for failures to aid classification and debugging
    detail = (result.stderr or "").strip()
    if not detail:
        return base

    preview = truncate_detail_keep_tail(detail, max_characters=limit)
    # Avoid appending if the diagnostic content is already part of the base summary.
    # We check the tail specifically as failures often share a common prefix but
    # have unique suffixes.
    if detail[-limit:].strip() in base:
        return base

    return f"{base} {preview}".strip()


def build_failure_summary(
    *,
    summary: str | None = None,
    final_message: str | None = None,
) -> str:
    """Construct a unified failure summary from raw output and structured messages."""
    # Ensure we only perform string operations if the inputs are actual strings.
    # This prevents TypeErrors in tests when MagicMocks are passed as result fields.
    safe_summary = summary if isinstance(summary, str) else None
    safe_message = final_message if isinstance(final_message, str) else None

    base = (safe_summary or "").strip()
    if not safe_message:
        return base

    msg = safe_message.strip()
    # Avoid duplication if the structured message is already contained in the raw summary
    # or vice-versa (which can happen if the runtime loop uses the final message as summary).
    if not msg:
        return base
    if not base:
        return msg
    if msg in base:
        return base
    if base in msg:
        return msg

    return f"{msg} {base}".strip()
