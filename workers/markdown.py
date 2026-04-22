"""Shared markdown-rendering helpers for worker prompts."""

from __future__ import annotations

import re

DEFAULT_MARKDOWN_FENCE_MINIMUM = 4


def markdown_fence_for_content(
    content: str,
    *,
    minimum: int = DEFAULT_MARKDOWN_FENCE_MINIMUM,
) -> str:
    """Return a backtick fence that cannot collide with backtick runs in content."""
    max_run = 0
    for match in re.finditer(r"`+", content):
        max_run = max(max_run, len(match.group(0)))
    return "`" * max(minimum, max_run + 1)
