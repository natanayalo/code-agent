"""Helpers for worker-local structured self-review passes."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from apps.observability import (
    SPAN_KIND_AGENT,
    set_span_input_output,
    start_optional_span,
    with_span_kind,
)
from tools import ToolPermissionLevel, ToolRegistry
from tools.numeric import coerce_non_negative_int_like
from workers.base import ArtifactReference, WorkerCommand
from workers.cli_runtime import (
    CliRuntimeAdapter,
    CliRuntimeBudgetLedger,
    CliRuntimeExecutionResult,
    CliRuntimeSettings,
    ShellSessionProtocol,
    run_cli_runtime_loop,
)
from workers.constants import (
    DEFAULT_DIFF_TIMEOUT_SECONDS as DEFAULT_SELF_REVIEW_DIFF_TIMEOUT_SECONDS,
)
from workers.post_run_lint import merge_post_run_lint_results
from workers.prompt_review import build_review_prompt
from workers.review import ReviewResult
from workers.self_review_packet import build_targeted_review_context_packet

TRACER_NAME: Final[str] = "workers.self_review"

DEFAULT_SELF_REVIEW_MAX_FIX_ITERATIONS = 2
DEFAULT_SELF_REVIEW_DIFF_MAX_CHARACTERS = 12000


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
    resolved_repo_path = repo_path or Path(".")
    return build_review_prompt(
        workspace_path=resolved_repo_path,
        review_context_packet=review_context_packet,
        reviewer_kind="worker_self_review",
        task_text=task_text,
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


def run_shared_self_review_fix_loop(
    *,
    execution: CliRuntimeExecutionResult,
    task_text: str,
    constraints: Mapping[str, Any],
    runtime_adapter: CliRuntimeAdapter,
    runtime_settings: CliRuntimeSettings,
    system_prompt: str,
    repo_path: Path,
    files_changed: list[str],
    lint_format_result: dict[str, Any],
    lint_format_artifacts: list[ArtifactReference],
    post_run_lint_collector: Callable[
        [CliRuntimeExecutionResult, list[str]],
        tuple[list[str], dict[str, Any], list[ArtifactReference]],
    ],
    tool_registry: ToolRegistry,
    granted_permission: ToolPermissionLevel,
    session: ShellSessionProtocol,
    cancel_token: Callable[[], bool] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    model_name: str | None = None,
    adapter_failure_log_message: str | None = None,
    adapter_failure_logger: logging.Logger | None = None,
    check_cancel_before_review: bool = False,
) -> tuple[ReviewResult | None, list[str], dict[str, Any], list[ArtifactReference]]:
    """Run worker self-review with bounded fix-loop retries, mutating execution in place."""
    review_result: ReviewResult | None = None
    if execution.status != "success" or should_skip_self_review(constraints):
        return review_result, files_changed, lint_format_result, lint_format_artifacts

    max_fix_iterations = resolve_self_review_max_fix_iterations(constraints)
    for review_attempt in range(max_fix_iterations + 1):
        if check_cancel_before_review and cancel_token and cancel_token():
            break

        diff_text = collect_diff_for_review(
            repo_path,
            timeout_seconds=runtime_settings.command_timeout_seconds,
        )
        review_prompt = build_self_review_prompt(
            task_text=task_text,
            worker_summary=execution.summary,
            files_changed=files_changed,
            diff_text=diff_text,
            repo_path=repo_path,
            commands_run=execution.commands_run,
        )

        try:
            turn_name = f"Turn {review_attempt + 1} (Self-Review)"
            if model_name:
                turn_name = f"{model_name} Turn {review_attempt + 1} (Self-Review)"

            with start_optional_span(
                tracer_name=TRACER_NAME,
                span_name=turn_name,
                attributes=with_span_kind(SPAN_KIND_AGENT),
                task_id=task_id,
                session_id=session_id,
            ):
                set_span_input_output(input_data=review_prompt)
                review_step = runtime_adapter.next_step(
                    (),
                    prompt_override=review_prompt,
                    working_directory=repo_path,
                    task_id=task_id,
                    session_id=session_id,
                )
                if review_step.kind == "final" and review_step.final_output:
                    set_span_input_output(input_data=None, output_data=review_step.final_output)
                elif review_step.kind == "tool_call":
                    set_span_input_output(
                        input_data=None,
                        output_data=f"Executing {review_step.tool_name}",
                    )
        except Exception as exc:
            if adapter_failure_log_message and adapter_failure_logger is not None:
                adapter_failure_logger.warning(adapter_failure_log_message, exc_info=exc)
            review_result = fallback_no_findings_review(
                "Worker self-review failed to return a structured payload."
            )
            break

        if review_step.kind != "final" or review_step.final_output is None:
            review_result = fallback_no_findings_review(
                "Worker self-review returned a non-final response."
            )
            break

        parsed_review_result = parse_review_result(review_step.final_output)
        if parsed_review_result is None:
            review_result = fallback_no_findings_review(
                "Worker self-review returned an invalid structured payload."
            )
            break

        review_result = parsed_review_result
        if review_result.outcome == "no_findings":
            break
        if review_attempt >= max_fix_iterations:
            break

        follow_up_settings = remaining_runtime_settings(
            runtime_settings,
            budget_ledger=execution.budget_ledger,
        )
        if follow_up_settings is None:
            execution.status = "failure"
            execution.summary = (
                "CLI runtime exhausted its remaining budget before applying self-review fixes."
            )
            execution.stop_reason = "budget_exceeded"
            break

        follow_up_execution = run_cli_runtime_loop(
            runtime_adapter,
            session,
            system_prompt=build_fix_loop_prompt(
                base_system_prompt=system_prompt,
                review_result=review_result,
            ),
            settings=follow_up_settings,
            tool_registry=tool_registry,
            granted_permission=granted_permission,
            working_directory=repo_path,
            cancel_token=cancel_token,
            task_id=task_id,
            session_id=session_id,
            model_name=model_name,
        )
        merge_budget_ledgers(execution.budget_ledger, follow_up_execution.budget_ledger)
        execution.commands_run.extend(follow_up_execution.commands_run)
        execution.messages.extend(follow_up_execution.messages)
        execution.status = follow_up_execution.status
        execution.summary = follow_up_execution.summary
        execution.stop_reason = follow_up_execution.stop_reason
        execution.permission_decision = follow_up_execution.permission_decision
        if execution.status != "success":
            break

        files_changed, new_lint_format_result, new_lint_format_artifacts = post_run_lint_collector(
            execution,
            files_changed,
        )
        lint_format_result = merge_post_run_lint_results(
            lint_format_result,
            new_lint_format_result,
        )
        lint_format_artifacts.extend(new_lint_format_artifacts)

    return review_result, files_changed, lint_format_result, lint_format_artifacts


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
