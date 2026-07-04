"""Shared redaction helpers for user-marked private text."""

from __future__ import annotations

import re
from typing import Any

PRIVATE_TAG_REPLACEMENT = "[redacted-private]"
_PRIVATE_TAG_PATTERN = re.compile(r"<private>(.*?)</private>", re.DOTALL | re.IGNORECASE)
_CONTAINER_TYPES = (dict, list, tuple, set, frozenset)
_REDACTABLE_TYPES = (*_CONTAINER_TYPES, str)


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
            redacted_key, key_redacted = (
                redact_private_tags(key) if isinstance(key, str) else (key, False)
            )
            redacted_value, value_redacted = (
                redact_private_tags_recursive(value)
                if isinstance(value, _REDACTABLE_TYPES)
                else (value, False)
            )
            redacted_dict[redacted_key] = redacted_value
            any_redacted = any_redacted or key_redacted or value_redacted
        return redacted_dict, any_redacted
    if isinstance(data, list | tuple | set | frozenset):
        redacted_items = []
        any_redacted = False
        for item in data:
            redacted_item, redacted = (
                redact_private_tags_recursive(item)
                if isinstance(item, _REDACTABLE_TYPES)
                else (item, False)
            )
            redacted_items.append(redacted_item)
            any_redacted = any_redacted or redacted
        if isinstance(data, tuple):
            return tuple(redacted_items), any_redacted
        if isinstance(data, set):
            return set(redacted_items), any_redacted
        if isinstance(data, frozenset):
            return frozenset(redacted_items), any_redacted
        return redacted_items, any_redacted
    return data, False
