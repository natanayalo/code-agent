"""Helpers for worker-local structured self-review passes."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.numeric import coerce_non_negative_int_like
from workers.cli_runtime import CliRuntimeBudgetLedger, CliRuntimeSettings
from workers.review import ReviewResult

DEFAULT_SELF_REVIEW_MAX_FIX_ITERATIONS = 2
DEFAULT_SELF_REVIEW_DIFF_MAX_CHARACTERS = 12000
DEFAULT_SELF_REVIEW_DIFF_TIMEOUT_SECONDS = 15


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
) -> str:
    """Build the focused prompt for one self-review pass."""
    changed_files_block = "\n".join(f"- {path}" for path in files_changed) or "- <none>"
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
            "- Keep findings bounded and specific to the supplied diff.",
            "",
            "## Task Objective",
            task_text,
            "",
            "## Worker Summary",
            worker_summary,
            "",
            "## Changed Files",
            changed_files_block,
            "",
            "## Diff Snapshot",
            "```diff",
            diff_text,
            "```",
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
