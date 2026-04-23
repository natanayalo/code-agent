"""Context packing helpers for independent and self-review passes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from workers.base import WorkerCommand
from workers.markdown import markdown_fence_for_content

DEFAULT_REVIEW_PACKET_MAX_CHARACTERS = 12000
DEFAULT_REVIEW_PACKET_MAX_FILES = 12
DEFAULT_REVIEW_PACKET_MAX_COMMANDS = 24
DEFAULT_REVIEW_PACKET_MIN_DIFF_BUDGET = 1200
DEFAULT_REVIEW_PACKET_MIN_BASE_BUDGET = 256


def _truncate_at_line_boundary(
    text: str,
    *,
    max_characters: int,
    marker: str = "\n... (truncated)",
) -> str:
    """Trim text to a bounded size while preferring whole-line boundaries."""
    if max_characters <= 0:
        return ""
    if len(text) <= max_characters:
        return text
    marker_length = len(marker)
    if max_characters <= marker_length:
        return marker[:max_characters]
    keep_budget = max_characters - marker_length
    prefix = text[:keep_budget]
    if "\n" in prefix:
        prefix = prefix.rsplit("\n", 1)[0]
    prefix = prefix.rstrip()
    if not prefix:
        prefix = text[:keep_budget].rstrip()
    return f"{prefix}{marker}"


def pack_reviewer_context(
    *,
    task_text: str,
    worker_summary: str,
    files_changed: Sequence[str],
    diff_text: str,
    commands_run: Sequence[WorkerCommand] = (),
    verifier_report: Mapping[str, Any] | None = None,
    session_state: Mapping[str, Any] | None = None,
    max_characters: int = DEFAULT_REVIEW_PACKET_MAX_CHARACTERS,
) -> str:
    """Assemble a deterministic, bounded review packet centered on changed code."""
    # This is a specialized version of the self-review packet builder
    # that can be used by both the orchestrator and internal worker loops.

    normalized_files = sorted({path.strip() for path in files_changed if path.strip()})
    if len(normalized_files) > DEFAULT_REVIEW_PACKET_MAX_FILES:
        visible_files = normalized_files[:DEFAULT_REVIEW_PACKET_MAX_FILES]
        omitted_count = len(normalized_files) - DEFAULT_REVIEW_PACKET_MAX_FILES
        changed_files_lines = [*(f"- {path}" for path in visible_files)]
        changed_files_lines.append(f"- ... {omitted_count} more files omitted")
        changed_files_block = "\n".join(changed_files_lines)
    else:
        changed_files_block = "\n".join(f"- {path}" for path in normalized_files) or "- <none>"

    # Simple command summary
    command_lines = []
    for cmd in commands_run[:DEFAULT_REVIEW_PACKET_MAX_COMMANDS]:
        exit_label = "unknown" if cmd.exit_code is None else str(cmd.exit_code)
        command_lines.append(f"- exit={exit_label} | {cmd.command}")
    if len(commands_run) > DEFAULT_REVIEW_PACKET_MAX_COMMANDS:
        command_lines.append(
            f"- ... {len(commands_run) - DEFAULT_REVIEW_PACKET_MAX_COMMANDS} more commands omitted"
        )
    command_summary_block = "\n".join(command_lines) or "- <none>"

    diff_fence = markdown_fence_for_content(diff_text)

    sections: list[str] = [
        "### Task Objective",
        task_text.strip() or "<empty>",
        "",
        "### Worker Summary",
        worker_summary.strip() or "<empty>",
        "",
        "### Changed Files",
        changed_files_block,
        "",
        "### Command Summary",
        command_summary_block,
    ]

    if verifier_report:
        report_json = json.dumps(dict(verifier_report), indent=2, sort_keys=True)
        sections.extend(["", "### Verifier Report", report_json])

    if session_state:
        state_json = json.dumps(dict(session_state), indent=2, sort_keys=True)
        sections.extend(["", "### Compact Session State", state_json])

    base_packet = "\n".join(sections).strip()

    diff_header = "\n\n### Diff Excerpt\n"

    truncation_marker = "\n... (truncated)"
    diff_open = f"{diff_fence}diff\n"
    diff_close = f"\n{diff_fence}"
    diff_overhead = len(diff_header) + len(diff_open) + len(diff_close)
    content_budget = max(max_characters - diff_overhead, 0)
    min_base_budget = min(DEFAULT_REVIEW_PACKET_MIN_BASE_BUDGET, content_budget)
    max_diff_budget = max(content_budget - min_base_budget, 0)
    diff_reserved_budget = min(
        max(DEFAULT_REVIEW_PACKET_MIN_DIFF_BUDGET, max_characters // 3),
        max_diff_budget,
    )
    base_budget = max(content_budget - diff_reserved_budget, 0)
    base_packet = _truncate_at_line_boundary(
        base_packet,
        max_characters=base_budget,
        marker=truncation_marker,
    )

    full_diff_block = f"{diff_open}{diff_text}{diff_close}"
    if len(base_packet) + len(diff_header) + len(full_diff_block) <= max_characters:
        return base_packet + diff_header + full_diff_block

    remaining_diff_budget = (
        max_characters
        - len(base_packet)
        - len(diff_header)
        - len(diff_open)
        - len(diff_close)
        - len(truncation_marker)
    )
    if remaining_diff_budget <= 0:
        return base_packet + diff_header + diff_open + truncation_marker + diff_close

    truncated_diff = _truncate_at_line_boundary(
        diff_text,
        max_characters=remaining_diff_budget,
        marker=truncation_marker,
    )

    return base_packet + diff_header + diff_open + truncated_diff + diff_close
