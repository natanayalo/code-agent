"""Budget bookkeeping helpers for the shared CLI runtime."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from apps.observability import set_span_status_from_outcome
from tools import ToolDefinition, ToolPermissionDecision
from workers.base import WorkerCommand
from workers.cli_runtime_types import (
    CliRuntimeBudgetLedger,
    CliRuntimeExecutionResult,
    CliRuntimeMessage,
    CliRuntimeSettings,
)


def _retry_command_key(command: str) -> tuple[str, ...]:
    """Normalize a command for retry-budget comparisons."""
    try:
        return tuple(shlex.split(command, posix=True))
    except ValueError:
        return tuple(command.split())


def _retry_command_budget_key(command_key: tuple[str, ...]) -> str:
    """Render a stable dictionary key for per-command retry tracking."""
    return shlex.join(command_key)


def _resolve_command_timeout_seconds(
    *,
    tool: ToolDefinition,
    started_at: float,
    settings: CliRuntimeSettings,
    clock: Callable[[], float],
) -> int:
    """Clamp per-command timeout by tool policy and the overall worker budget."""
    elapsed_seconds = clock() - started_at
    remaining_seconds = max(int(settings.worker_timeout_seconds - elapsed_seconds), 1)
    return min(settings.command_timeout_seconds, tool.timeout_seconds, remaining_seconds)


def _build_budget_ledger(settings: CliRuntimeSettings) -> CliRuntimeBudgetLedger:
    """Initialize budget tracking from the resolved runtime settings."""
    return CliRuntimeBudgetLedger(
        max_iterations=settings.max_iterations,
        max_tool_calls=settings.max_tool_calls,
        max_shell_commands=settings.max_shell_commands,
        max_retries=settings.max_retries,
        max_verifier_passes=settings.max_verifier_passes,
    )


def _update_budget_ledger(
    ledger: CliRuntimeBudgetLedger,
    *,
    started_at: float,
    clock: Callable[[], float],
    iterations_used: int | None = None,
) -> None:
    """Refresh wall-clock and iteration usage before returning a runtime result."""
    ledger.wall_clock_seconds = max(clock() - started_at, 0.0)
    if iterations_used is not None:
        ledger.iterations_used = max(ledger.iterations_used, iterations_used)


@dataclass(frozen=True)
class _ResultContext:
    """Encapsulates common runtime loop state for final result construction."""

    started_at: float
    clock: Callable[[], float]
    budget_ledger: CliRuntimeBudgetLedger
    commands_run: list[WorkerCommand]
    messages: list[CliRuntimeMessage]
    permission_decision: ToolPermissionDecision | None = None


def _finalize_execution_result(
    context: _ResultContext,
    *,
    status: Literal["success", "failure", "error"],
    summary: str,
    stop_reason: Literal[
        "final_answer",
        "max_iterations",
        "stalled_in_inspection",
        "exploration_exhausted",
        "no_progress_before_budget",
        "worker_timeout",
        "budget_exceeded",
        "context_window",
        "permission_required",
        "shell_error",
        "adapter_error",
    ],
    iteration: int,
) -> CliRuntimeExecutionResult:
    """Build a consistent runtime result and refresh the budget ledger."""
    _update_budget_ledger(
        context.budget_ledger,
        started_at=context.started_at,
        clock=context.clock,
        iterations_used=iteration,
    )
    set_span_status_from_outcome(status, summary)
    return CliRuntimeExecutionResult(
        status=status,
        summary=summary,
        stop_reason=stop_reason,
        commands_run=context.commands_run,
        messages=context.messages,
        budget_ledger=context.budget_ledger,
        permission_decision=context.permission_decision,
    )
