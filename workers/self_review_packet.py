from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from workers.base import WorkerCommand
from workers.markdown import markdown_fence_for_content

DEFAULT_REVIEW_PACKET_MAX_CHARACTERS = 12000
DEFAULT_REVIEW_PACKET_MAX_FILES = 8
DEFAULT_REVIEW_PACKET_MAX_COMMANDS = 12
DEFAULT_REVIEW_PACKET_CODE_WINDOW_RADIUS = 3
DEFAULT_REVIEW_PACKET_MAX_CODE_LINES = 120
DEFAULT_REVIEW_PACKET_MAX_WINDOWS_PER_FILE = 8
DEFAULT_REVIEW_PACKET_MAX_FILE_BYTES = 2 * 1024 * 1024


def build_targeted_review_context_packet(
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
    normalized_files = sorted({path.strip() for path in files_changed if path.strip()})
    changed_files_block = "\n".join(f"- {path}" for path in normalized_files) or "- <none>"
    command_summary_block = _summarize_commands(commands_run)
    diff_fence = markdown_fence_for_content(diff_text)
    truncated_diff_text = _truncate_block(diff_text, max_characters // 2)
    diff_block = f"{diff_fence}diff\n{truncated_diff_text}\n{diff_fence}"
    code_windows_block = _build_changed_file_windows(
        repo_path=repo_path,
        changed_files=normalized_files[:DEFAULT_REVIEW_PACKET_MAX_FILES],
        diff_text=diff_text,
    )

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
        sections.extend(
            [
                "",
                "### Verifier Report",
                _truncate_block(
                    json.dumps(dict(verifier_report), indent=2, sort_keys=True),
                    max_characters // 6,
                ),
            ]
        )
    if session_state:
        sections.extend(
            [
                "",
                "### Compact Session State",
                _truncate_block(
                    json.dumps(dict(session_state), indent=2, sort_keys=True),
                    max_characters // 6,
                ),
            ]
        )

    sections.extend(
        [
            "",
            "### Diff Excerpt",
            diff_block,
            "",
            "### Changed-File Code Windows",
            code_windows_block,
        ]
    )
    packet = "\n".join(sections).strip()
    return _truncate_review_packet(packet, max_characters, diff_fence=diff_fence)


def _truncate_block(text: str, limit: int) -> str:
    """Return a bounded text block with an explicit truncation marker when required."""
    if limit <= 0:
        return "[truncated]"
    if len(text) <= limit:
        return text
    truncated = text[:limit].rstrip()
    return f"{truncated}\n[truncated to {limit} characters]"


def _truncate_review_packet(packet: str, limit: int, *, diff_fence: str) -> str:
    """Truncate packet while keeping diff markdown fences balanced when possible."""
    if len(packet) <= limit:
        return packet

    marker = f"\n[truncated to {limit} characters]"
    truncated = packet[:limit].rstrip()
    diff_header = "### Diff Excerpt"
    diff_start = truncated.find(diff_header)
    if diff_start == -1:
        return f"{truncated}{marker}"

    diff_section = truncated[diff_start:]
    if diff_section.count(diff_fence) % 2 == 0:
        return f"{truncated}{marker}"

    closing_fence = f"\n{diff_fence}"
    if limit <= len(marker) + len(closing_fence):
        return _truncate_block(packet, limit)

    budget = limit - len(marker) - len(closing_fence)
    balanced = packet[:budget].rstrip()
    return f"{balanced}{closing_fence}{marker}"


def _summarize_commands(commands_run: Sequence[WorkerCommand]) -> str:
    """Summarize executed commands with exit metadata in deterministic order."""
    if not commands_run:
        return "- <none>"

    lines: list[str] = []
    for command in commands_run[:DEFAULT_REVIEW_PACKET_MAX_COMMANDS]:
        exit_label = "unknown" if command.exit_code is None else str(command.exit_code)
        lines.append(f"- exit={exit_label} | {command.command}")

    if len(commands_run) > DEFAULT_REVIEW_PACKET_MAX_COMMANDS:
        lines.append(
            f"- ... {len(commands_run) - DEFAULT_REVIEW_PACKET_MAX_COMMANDS} more commands omitted"
        )
    return "\n".join(lines)


def _extract_diff_line_hints(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Parse git diff hunks into per-file changed ranges on the new-file side."""
    active_path: str | None = None
    line_hints: dict[str, list[tuple[int, int]]] = {}
    hunk_pattern = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            active_path = _normalize_diff_new_path(line[4:].strip())
            if active_path is None:
                active_path = None
                continue
            line_hints.setdefault(active_path, [])
            continue
        if not active_path or not line.startswith("@@"):
            continue
        match = hunk_pattern.search(line)
        if match is None:
            continue
        old_start = int(match.group(1))
        new_start = int(match.group(3))
        new_count = int(match.group(4) or "1")
        if new_count > 0:
            range_start = max(1, new_start)
            range_end = max(range_start, new_start + new_count - 1)
        else:
            # Pure deletions have no new-file span; anchor at the deletion point.
            anchor = max(1, new_start if new_start > 0 else old_start)
            range_start = anchor
            range_end = anchor
        line_hints.setdefault(active_path, []).append((range_start, range_end))

    return {path: _merge_line_ranges(ranges) for path, ranges in line_hints.items() if ranges}


def _get_changed_file_lines(
    repo_path: Path,
    file_path: str,
    windows: list[str],
) -> list[str] | None:
    """Read file lines safely, appending skip/error messages to windows if unable."""
    resolved_path = (repo_path / file_path).resolve()
    try:
        resolved_path.relative_to(repo_path.resolve())
    except ValueError:
        windows.append(f"- {file_path}: [skipped: outside repo path]")
        return None
    if not resolved_path.is_file():
        windows.append(f"- {file_path}: [missing]")
        return None
    try:
        file_size = resolved_path.stat().st_size
    except OSError as exc:
        windows.append(f"- {file_path}: [stat failed: {exc}]")
        return None
    if file_size > DEFAULT_REVIEW_PACKET_MAX_FILE_BYTES:
        windows.append(
            f"- {file_path}: [skipped: file size {file_size} bytes exceeds "
            f"{DEFAULT_REVIEW_PACKET_MAX_FILE_BYTES}-byte limit]"
        )
        return None
    try:
        return resolved_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        windows.append(f"- {file_path}: [read failed: {exc}]")
        return None


def _format_file_window_blocks(
    file_path: str,
    file_lines: list[str],
    window_ranges: list[tuple[int, int]],
    total_lines: int,
    windows: list[str],
) -> int:
    """Format individual code windows for a file and return the updated total lines."""
    file_window_count = 0
    for first_line, last_line in window_ranges:
        if total_lines >= DEFAULT_REVIEW_PACKET_MAX_CODE_LINES:
            break
        if file_window_count >= DEFAULT_REVIEW_PACKET_MAX_WINDOWS_PER_FILE:
            windows.append(
                f"- {file_path}: [additional windows omitted after "
                f"{DEFAULT_REVIEW_PACKET_MAX_WINDOWS_PER_FILE}]"
            )
            break
        if last_line < first_line:
            continue
        remaining_lines = DEFAULT_REVIEW_PACKET_MAX_CODE_LINES - total_lines
        if remaining_lines <= 0:
            break
        clipped_last_line = min(last_line, first_line + remaining_lines - 1)
        if clipped_last_line < first_line:
            break

        section_lines = file_lines[first_line - 1 : clipped_last_line]
        numbered_lines = "\n".join(
            f"{line_number:04d}: {line_text}"
            for line_number, line_text in enumerate(section_lines, start=first_line)
        )
        code_fence = markdown_fence_for_content(numbered_lines)
        windows.append(
            "\n".join(
                [
                    f"- {file_path} ({first_line}-{clipped_last_line})",
                    f"{code_fence}text",
                    numbered_lines or "<empty>",
                    code_fence,
                ]
            )
        )
        total_lines += len(section_lines)
        file_window_count += 1
        if clipped_last_line < last_line:
            windows.append(
                f"- {file_path}: [window truncated to respect "
                f"{DEFAULT_REVIEW_PACKET_MAX_CODE_LINES}-line packet budget]"
            )
            break

    return total_lines


def _build_changed_file_windows(
    *,
    repo_path: Path | None,
    changed_files: Sequence[str],
    diff_text: str,
) -> str:
    """Collect compact line-numbered code windows around changed diff ranges."""
    if repo_path is None:
        return "<workspace unavailable>"
    if not changed_files:
        return "<none>"

    line_hints_by_file = _extract_diff_line_hints(diff_text)
    windows: list[str] = []
    total_lines = 0

    for file_path in changed_files:
        if total_lines >= DEFAULT_REVIEW_PACKET_MAX_CODE_LINES:
            break
        file_lines = _get_changed_file_lines(repo_path, file_path, windows)
        if file_lines is None:
            continue

        hint_ranges = line_hints_by_file.get(file_path, [])
        if hint_ranges:
            window_ranges = [
                (
                    max(1, start - DEFAULT_REVIEW_PACKET_CODE_WINDOW_RADIUS),
                    min(len(file_lines), end + DEFAULT_REVIEW_PACKET_CODE_WINDOW_RADIUS),
                )
                for start, end in hint_ranges
            ]
            window_ranges = _merge_line_ranges(window_ranges)
        else:
            window_ranges = [(1, min(len(file_lines), 20))]

        if not window_ranges:
            windows.append(f"- {file_path}: [empty]")
            continue

        total_lines = _format_file_window_blocks(
            file_path=file_path,
            file_lines=file_lines,
            window_ranges=window_ranges,
            total_lines=total_lines,
            windows=windows,
        )

    return "\n".join(windows) if windows else "<none>"


def _merge_line_ranges(
    ranges: Sequence[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent inclusive line ranges."""
    if not ranges:
        return []
    normalized = sorted((min(start, end), max(start, end)) for start, end in ranges)
    merged: list[tuple[int, int]] = [normalized[0]]
    for start, end in normalized[1:]:
        current_start, current_end = merged[-1]
        if start <= current_end + 1:
            merged[-1] = (current_start, max(current_end, end))
            continue
        merged.append((start, end))
    return merged


def _normalize_diff_new_path(raw_path: str) -> str | None:
    """Normalize a diff `+++` path token across prefixed, noprefix, and quoted forms."""
    if raw_path == "/dev/null":
        return None

    candidate = raw_path
    if candidate.startswith('"') and candidate.endswith('"') and len(candidate) >= 2:
        candidate = candidate[1:-1]
        candidate = candidate.replace("\\\\", "\\").replace('\\"', '"')
    if candidate.startswith("b/"):
        candidate = candidate[2:]
    return candidate.strip() or None
