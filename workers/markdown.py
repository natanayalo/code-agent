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


def unwrap_markdown_json_fence(content: str) -> str:
    """Extract JSON payload from a markdown code fence or return raw content."""
    stripped = content.strip()
    if not stripped:
        return stripped
    # Match ```json ... ``` or ``` ... ```
    fenced_matches = re.findall(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        re.DOTALL | re.IGNORECASE,
    )
    if fenced_matches:
        return fenced_matches[-1].strip()
    return stripped
