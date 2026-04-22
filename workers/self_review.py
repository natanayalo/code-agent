"""Helpers for worker-local structured self-review passes."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.numeric import coerce_non_negative_int_like
from workers.base import WorkerCommand
from workers.cli_runtime import CliRuntimeBudgetLedger, CliRuntimeSettings
from workers.review import ReviewResult

DEFAULT_SELF_REVIEW_MAX_FIX_ITERATIONS = 2
DEFAULT_SELF_REVIEW_DIFF_MAX_CHARACTERS = 12000
DEFAULT_SELF_REVIEW_DIFF_TIMEOUT_SECONDS = 15
DEFAULT_REVIEW_PACKET_MAX_CHARACTERS = 12000
DEFAULT_REVIEW_PACKET_MAX_FILES = 8
DEFAULT_REVIEW_PACKET_MAX_COMMANDS = 12
DEFAULT_REVIEW_PACKET_CODE_WINDOW_RADIUS = 3
DEFAULT_REVIEW_PACKET_MAX_CODE_LINES = 120
DEFAULT_REVIEW_PACKET_MAX_WINDOWS_PER_FILE = 8
DEFAULT_REVIEW_PACKET_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MARKDOWN_FENCE = "````"


def should_skip_self_review(constraints: Mapping[str, Any]) -> bool:
    """Return True when worker constraints explicitly disable self-review."""
    skip_flag = constraints.get("skip_self_review")
    if isinstance(skip_flag, bool):
        return skip_flag
    if isinstance(skip_flag, str):
        return skip_flag.strip().lower() in {"1", "true", "yes", "on"}

    enabled_flag = constraints.get("self_review_enabled")
    if isinstance(enabled_flag, bool):
        return not enabled_flag
    if isinstance(enabled_flag, str):
        normalized = enabled_flag.strip().lower()
        if normalized in {"0", "false", "no", "off"}:
            return True
    return False


def resolve_self_review_max_fix_iterations(
    constraints: Mapping[str, Any],
    *,
    default: int = DEFAULT_SELF_REVIEW_MAX_FIX_ITERATIONS,
) -> int:
    """Resolve bounded fix-loop retries from worker constraints."""
    parsed = coerce_non_negative_int_like(constraints.get("self_review_max_fix_iterations"))
    if parsed is None:
        return default
    return min(parsed, DEFAULT_SELF_REVIEW_MAX_FIX_ITERATIONS)


def collect_diff_for_review(
    repo_path: Path,
    *,
    timeout_seconds: int = DEFAULT_SELF_REVIEW_DIFF_TIMEOUT_SECONDS,
    max_characters: int = DEFAULT_SELF_REVIEW_DIFF_MAX_CHARACTERS,
) -> str:
    """Collect a bounded diff snapshot for review context."""
    command = [
        "git",
        "-C",
        str(repo_path),
        "diff",
        "--no-color",
        "--",
        ".",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"[diff collection timed out after {timeout_seconds}s]"
    except OSError as exc:
        return f"[diff collection failed: {exc}]"

    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "<empty stderr>"
        return f"[git diff failed with exit code {completed.returncode}: {stderr}]"

    payload = completed.stdout.strip() or "<no textual git diff output>"
    if len(payload) <= max_characters:
        return payload

    truncated = payload[:max_characters].rstrip()
    return f"{truncated}\n\n[diff truncated to {max_characters} characters]"


def build_self_review_prompt(
    *,
    task_text: str,
    worker_summary: str,
    files_changed: list[str],
    diff_text: str,
    repo_path: Path | None = None,
    commands_run: Sequence[WorkerCommand] = (),
    verifier_report: Mapping[str, Any] | None = None,
    session_state: Mapping[str, Any] | None = None,
) -> str:
    """Build the focused prompt for one self-review pass."""
    review_context_packet = build_targeted_review_context_packet(
        task_text=task_text,
        worker_summary=worker_summary,
        files_changed=files_changed,
        diff_text=diff_text,
        repo_path=repo_path,
        commands_run=commands_run,
        verifier_report=verifier_report,
        session_state=session_state,
    )
    return "\n".join(
        [
            "You are running a bounded worker self-review before final completion.",
            "Review the delivered task outcome and code diff.",
            "Evaluate:",
            "1. Does the diff satisfy the task objective?",
            "2. Are there unintended changes?",
            "3. Are there obvious logical errors?",
            "4. Are relevant tests missing?",
            "Return exactly one JSON object with no markdown fences and no prose.",
            "Schema:",
            "{"
            '"reviewer_kind":"worker_self_review",'
            '"summary":"string",'
            '"confidence":0.0,'
            '"outcome":"no_findings|findings",'
            '"findings":[{'
            '"severity":"low|medium|high|critical",'
            '"category":"string",'
            '"confidence":0.0,'
            '"file_path":"string",'
            '"line_start":1,'
            '"line_end":1,'
            '"title":"string",'
            '"why_it_matters":"string",'
            '"evidence":"string|null",'
            '"suggested_fix":"string|null"'
            "}]"
            "}",
            "Rules:",
            "- Use outcome `no_findings` with an empty `findings` list when nothing "
            "actionable exists.",
            "- Use outcome `findings` only when at least one concrete actionable finding exists.",
            "- Keep findings bounded and specific to the supplied review context packet.",
            "",
            "## Review Context Packet",
            review_context_packet,
        ]
    )


def build_fix_loop_prompt(
    *,
    base_system_prompt: str,
    review_result: ReviewResult,
) -> str:
    """Build a focused follow-up prompt for fixing self-review findings."""
    findings_payload = review_result.model_dump(mode="json")
    findings_json = json.dumps(findings_payload, indent=2, sort_keys=True)
    return "\n".join(
        [
            base_system_prompt,
            "",
            "## Self-Review Fix Pass",
            "The previous self-review found actionable issues.",
            "Fix only the findings below with the smallest safe edits, then summarize changes.",
            "If a finding is not valid, explain briefly in your final output.",
            "```json",
            findings_json,
            "```",
        ]
    )


def parse_review_result(raw_output: str) -> ReviewResult | None:
    """Parse a structured `ReviewResult` payload from model text output."""
    candidate = _extract_json_object(raw_output)
    if candidate is None:
        return None
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["reviewer_kind"] = "worker_self_review"
    try:
        return ReviewResult.model_validate(payload)
    except Exception:
        return None


def fallback_no_findings_review(summary: str) -> ReviewResult:
    """Build a safe, explicit no-findings review payload."""
    return ReviewResult(
        reviewer_kind="worker_self_review",
        summary=summary,
        confidence=0.0,
        outcome="no_findings",
        findings=[],
    )


def merge_budget_ledgers(
    existing: CliRuntimeBudgetLedger,
    additional: CliRuntimeBudgetLedger,
) -> None:
    """Accumulate additional loop usage into the original runtime budget ledger."""
    existing.iterations_used += additional.iterations_used
    existing.tool_calls_used += additional.tool_calls_used
    existing.shell_commands_used += additional.shell_commands_used
    existing.retries_used += additional.retries_used
    existing.wall_clock_seconds += additional.wall_clock_seconds
    for command_key, failures in additional.failed_command_attempts.items():
        existing.failed_command_attempts[command_key] = failures


def remaining_runtime_settings(
    base: CliRuntimeSettings,
    *,
    budget_ledger: CliRuntimeBudgetLedger,
) -> CliRuntimeSettings | None:
    """Return runtime settings clamped to the still-available global budget."""
    remaining_iterations = base.max_iterations - budget_ledger.iterations_used
    remaining_timeout = int(base.worker_timeout_seconds - budget_ledger.wall_clock_seconds)
    if remaining_iterations <= 0 or remaining_timeout <= 0:
        return None

    update_payload: dict[str, Any] = {
        "max_iterations": remaining_iterations,
        "worker_timeout_seconds": remaining_timeout,
    }

    if base.max_tool_calls is not None:
        remaining_tool_calls = base.max_tool_calls - budget_ledger.tool_calls_used
        if remaining_tool_calls < 0:
            return None
        update_payload["max_tool_calls"] = remaining_tool_calls

    if base.max_shell_commands is not None:
        remaining_shell_commands = base.max_shell_commands - budget_ledger.shell_commands_used
        if remaining_shell_commands < 0:
            return None
        update_payload["max_shell_commands"] = remaining_shell_commands

    if base.max_retries is not None:
        remaining_retries = base.max_retries - budget_ledger.retries_used
        if remaining_retries < 0:
            return None
        update_payload["max_retries"] = remaining_retries

    return base.model_copy(update=update_payload)


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
    diff_fence = _markdown_fence_for_content(diff_text)
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


def _extract_json_object(text: str) -> str | None:
    """Extract the first valid JSON object embedded in free-form text."""
    stripped = text.strip()
    search_from = 0
    while True:
        start = stripped.find("{", search_from)
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape_next = False
        end = -1
        for index, character in enumerate(stripped[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if in_string:
                if character == "\\":
                    escape_next = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            return None
        candidate = stripped[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            search_from = end + 1


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
    if "### Diff Excerpt" not in truncated or truncated.count(diff_fence) % 2 == 0:
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
        resolved_path = (repo_path / file_path).resolve()
        try:
            resolved_path.relative_to(repo_path.resolve())
        except ValueError:
            windows.append(f"- {file_path}: [skipped: outside repo path]")
            continue
        if not resolved_path.is_file():
            windows.append(f"- {file_path}: [missing]")
            continue
        try:
            file_size = resolved_path.stat().st_size
        except OSError as exc:
            windows.append(f"- {file_path}: [stat failed: {exc}]")
            continue
        if file_size > DEFAULT_REVIEW_PACKET_MAX_FILE_BYTES:
            windows.append(
                f"- {file_path}: [skipped: file size {file_size} bytes exceeds "
                f"{DEFAULT_REVIEW_PACKET_MAX_FILE_BYTES}-byte limit]"
            )
            continue
        try:
            file_lines = resolved_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            windows.append(f"- {file_path}: [read failed: {exc}]")
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
            code_fence = _markdown_fence_for_content(numbered_lines)
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


def _markdown_fence_for_content(content: str, *, minimum: int = len(DEFAULT_MARKDOWN_FENCE)) -> str:
    """Return a backtick fence that cannot collide with backtick runs in content."""
    max_run = 0
    for match in re.finditer(r"`+", content):
        max_run = max(max_run, len(match.group(0)))
    return "`" * max(minimum, max_run + 1)
