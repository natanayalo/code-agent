"""Shared redaction helpers for user-marked private text."""

from __future__ import annotations

import re
from typing import Any

PRIVATE_TAG_REPLACEMENT = "[redacted-private]"
_PRIVATE_TAG_PATTERN = re.compile(r"<private>(.*?)</private>", re.DOTALL | re.IGNORECASE)


def redact_private_tags(text: str) -> tuple[str, bool]:
    """Replace case-insensitive <private>...</private> blocks in text."""
    if not text:
        return text, False
    redacted, count = _PRIVATE_TAG_PATTERN.subn(PRIVATE_TAG_REPLACEMENT, text)
    return redacted, count > 0


def redact_private_tags_recursive(data: Any) -> tuple[Any, bool]:
    """Recursively redact private-tagged strings in container values."""
    if isinstance(data, str):
        return redact_private_tags(data)
    if isinstance(data, dict):
        redacted_dict = {}
        any_redacted = False
        for key, value in data.items():
            redacted_value, redacted = redact_private_tags_recursive(value)
            redacted_dict[key] = redacted_value
            any_redacted = any_redacted or redacted
        return redacted_dict, any_redacted
    if isinstance(data, list | tuple):
        redacted_items = []
        any_redacted = False
        for item in data:
            redacted_item, redacted = redact_private_tags_recursive(item)
            redacted_items.append(redacted_item)
            any_redacted = any_redacted or redacted
        if isinstance(data, tuple):
            return tuple(redacted_items), any_redacted
        return redacted_items, any_redacted
    return data, False
