"""Context packing helpers for independent and self-review passes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from workers.base import WorkerCommand
from workers.markdown import markdown_fence_for_content

DEFAULT_REVIEW_PACKET_MAX_CHARACTERS = 12000
DEFAULT_REVIEW_PACKET_MAX_FILES = 12
DEFAULT_REVIEW_PACKET_MAX_COMMANDS = 24


def pack_reviewer_context(
    *,
    task_text: str,
    worker_summary: str,
    files_changed: Sequence[str],
    diff_text: str,
    repo_path: Path | None = None,
    commands_run: Sequence[WorkerCommand] = (),
    verifier_report: Mapping[str, Any] | None = None,
    session_state: Mapping[str, Any] | None = None,
    max_characters: int = DEFAULT_REVIEW_PACKET_MAX_CHARACTERS,
) -> str:
    """Assemble a deterministic, bounded review packet centered on changed code."""
    # This is a specialized version of the self-review packet builder
    # that can be used by both the orchestrator and internal worker loops.

    normalized_files = sorted({path.strip() for path in files_changed if path.strip()})
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

    sections.extend(
        [
            "",
            "### Diff Excerpt",
            f"{diff_fence}diff\n{diff_text}\n{diff_fence}",
        ]
    )

    packet = "\n".join(sections).strip()

    if len(packet) <= max_characters:
        return packet

    # Naive truncation for now, T-115/T-116 might have more sophisticated logic
    # but we need to proceed with T-117.
    return packet[:max_characters].rstrip() + "\n... (truncated)"
