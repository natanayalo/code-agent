"""Stall detection and facade exports for CLI runtime tool turns."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from workers.base import WorkerCommand
from workers.cli_runtime_budget import _ResultContext, _update_budget_ledger
from workers.cli_runtime_tool_execution import ToolTurnResult, execute_tool_turn
from workers.cli_runtime_types import CliRuntimeExecutionResult


def _evaluate_stall_signals(  # type: ignore[no-untyped-def]
    *,
    settings,
    read_counts_by_file: dict[str, int],
    recent_iteration_signals: list[dict[str, Any]],
) -> bool:
    last_window = recent_iteration_signals[-settings.stall_window_iterations :]
    all_recent_read_only = len(last_window) == settings.stall_window_iterations and all(
        signal["read_only"] for signal in last_window
    )
    no_new_files_recently = len(last_window) == settings.stall_window_iterations and all(
        signal["new_files"] == 0 for signal in last_window
    )
    repeated_same_file_reads = any(
        count > settings.max_repeated_file_reads for count in read_counts_by_file.values()
    )
    return all_recent_read_only and (no_new_files_recently or repeated_same_file_reads)


def maybe_finalize_for_stall(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    settings,
    iteration: int,
    messages,
    commands_run: list[WorkerCommand],
    loop_state: Any,
    stall_correction_injected_at: int | None,
    started_at: float,
    clock: Callable[[], float],
):
    has_stall_signals = _evaluate_stall_signals(
        settings=settings,
        read_counts_by_file=loop_state.read_counts_by_file,
        recent_iteration_signals=loop_state.recent_iteration_signals,
    )
    if not has_stall_signals:
        return None, stall_correction_injected_at

    if stall_correction_injected_at is None and settings.stall_correction_turns > 0:
        messages.append(
            type(messages[0])(
                role="assistant",
                content=(
                    "Runtime corrective message: progress appears stalled. "
                    "Please stop rereading and do one now: "
                    "(1) concise plan, (2) first concrete edit, or "
                    "(3) final answer with findings and missing info."
                ),
            )
        )
        return None, iteration

    if (
        stall_correction_injected_at is not None
        and iteration - stall_correction_injected_at <= settings.stall_correction_turns
    ):
        return None, stall_correction_injected_at

    _update_budget_ledger(
        context.budget_ledger,
        started_at=started_at,
        clock=clock,
        iterations_used=iteration,
    )
    if loop_state.commands_with_writes == 0:
        return (
            CliRuntimeExecutionResult(
                status="failure",
                summary=(
                    "CLI runtime consumed iterations without meaningful task progress "
                    "before budget exhaustion."
                ),
                stop_reason="no_progress_before_budget",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=context.budget_ledger,
            ),
            stall_correction_injected_at,
        )

    return (
        CliRuntimeExecutionResult(
            status="failure",
            summary=(
                "CLI runtime stalled in repeated inspection without converging to "
                "concrete edits or a final answer."
            ),
            stop_reason="stalled_in_inspection",
            commands_run=commands_run,
            messages=messages,
            budget_ledger=context.budget_ledger,
        ),
        stall_correction_injected_at,
    )


__all__ = ["ToolTurnResult", "execute_tool_turn", "maybe_finalize_for_stall"]
