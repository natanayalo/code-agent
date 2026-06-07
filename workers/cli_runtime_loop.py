"""Core iterative CLI runtime loop implementation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from apps.observability import (
    SPAN_KIND_AGENT,
    set_optional_span_attribute,
    set_span_input_output,
    with_span_kind,
)
from sandbox.redact import SecretRedactor
from tools import McpToolClient, ToolPermissionLevel, ToolRegistry
from workers.base import WorkerCommand
from workers.cli_runtime_budget import (
    _build_budget_ledger,
    _finalize_execution_result,
    _ResultContext,
    _update_budget_ledger,
)
from workers.cli_runtime_context import _preflight_messages_for_adapter
from workers.cli_runtime_tool_turn import (
    execute_tool_turn,
    maybe_finalize_for_stall,
)
from workers.cli_runtime_tools import (
    _looks_like_tool_call_payload_text,
    _parse_runtime_step_from_text,
)
from workers.cli_runtime_types import (
    CliRuntimeAdapter,
    CliRuntimeExecutionResult,
    CliRuntimeMessage,
    CliRuntimeSettings,
    ShellSessionProtocol,
)

logger = logging.getLogger(__name__)

TRACER_NAME = "workers.cli_runtime"


def run_cli_runtime_loop_impl(
    adapter: CliRuntimeAdapter,
    session: ShellSessionProtocol,
    *,
    system_prompt: str,
    settings: CliRuntimeSettings,
    tool_registry: ToolRegistry | None = None,
    tool_client: McpToolClient,
    granted_permission: ToolPermissionLevel = ToolPermissionLevel.WORKSPACE_WRITE,
    clock: Callable[[], float] = perf_counter,
    working_directory: Path | None = None,
    cancel_token: Callable[[], bool] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    model_name: str | None = None,
    redactor: SecretRedactor | None = None,
    response_format: Literal["text", "json"] = "text",
    response_schema: dict[str, Any] | None = None,
    start_span: Callable[..., Any],
) -> CliRuntimeExecutionResult:
    """Drive the provider adapter through a bounded multi-turn shell loop."""
    started_at = clock()
    resolved_tool_client = tool_client or (
        tool_registry.mcp_client if tool_registry is not None else None
    )
    if resolved_tool_client is None:
        raise ValueError("A tool client is required to run the CLI runtime loop.")

    messages = [CliRuntimeMessage(role="system", content=system_prompt)]
    commands_run: list[WorkerCommand] = []
    budget_ledger = _build_budget_ledger(settings)
    from workers.cli_runtime_tool_execution import ToolExecutionState

    loop_state = ToolExecutionState()
    stall_correction_injected_at: int | None = None

    context = _ResultContext(
        started_at=started_at,
        clock=clock,
        budget_ledger=budget_ledger,
        commands_run=commands_run,
        messages=messages,
    )

    for iteration in range(1, settings.max_iterations + 1):
        result, stall_correction_injected_at = _run_single_turn_iteration(
            iteration=iteration,
            context=context,
            settings=settings,
            model_name=model_name,
            system_prompt=system_prompt,
            adapter=adapter,
            working_directory=working_directory,
            response_format=response_format,
            response_schema=response_schema,
            session=session,
            resolved_tool_client=resolved_tool_client,
            granted_permission=granted_permission,
            loop_state=loop_state,
            redactor=redactor,
            start_span=start_span,
            stall_correction_injected_at=stall_correction_injected_at,
            cancel_token=cancel_token,
            task_id=task_id,
            session_id=session_id,
        )
        if result is not None:
            return result

    return _finalize_exhausted_loop(
        context=context,
        settings=settings,
        loop_state=loop_state,
    )


def _finalize_exhausted_loop(
    *,
    context: _ResultContext,
    settings: CliRuntimeSettings,
    loop_state: Any,
) -> CliRuntimeExecutionResult:
    exhausted_without_progress = context.commands_run and loop_state.commands_with_writes == 0
    if exhausted_without_progress:
        summary = "CLI runtime consumed iterations without meaningful task progress before budget exhaustion."  # noqa: E501
        stop_reason = "no_progress_before_budget"
    else:
        summary = f"CLI runtime hit its max iteration budget ({settings.max_iterations}) before reaching a final answer."  # noqa: E501
        stop_reason = "max_iterations"
    return CliRuntimeExecutionResult(
        status="failure",
        summary=summary,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        commands_run=context.commands_run,
        messages=context.messages,
        budget_ledger=context.budget_ledger,
    )


class _PreflightFailure(Exception):
    """Raised when a turn cannot be sent to the adapter safely."""


def _maybe_finalize_for_cancel_or_timeout(
    *,
    context: _ResultContext,
    cancel_token: Callable[[], bool] | None,
    clock: Callable[[], float],
    started_at: float,
    settings: CliRuntimeSettings,
    iteration: int,
) -> CliRuntimeExecutionResult | None:
    if cancel_token is not None and cancel_token():
        return _finalize_execution_result(
            context,
            status="error",
            summary="CLI runtime loop was cancelled by the orchestrator timeout.",
            stop_reason="worker_timeout",
            iteration=iteration - 1,
        )
    if clock() - started_at >= settings.worker_timeout_seconds:
        return _finalize_execution_result(
            context,
            status="failure",
            summary=(
                "CLI runtime exceeded its worker timeout "
                f"({settings.worker_timeout_seconds}s) before reaching a final answer."
            ),
            stop_reason="worker_timeout",
            iteration=iteration - 1,
        )
    return None


def _get_adapter_step_safe(
    adapter: CliRuntimeAdapter,
    context: _ResultContext,
    messages: list[CliRuntimeMessage],
    settings: CliRuntimeSettings,
    system_prompt: str,
    model_name: str | None,
    iteration: int,
    working_directory: Path | None,
    response_format: Literal["text", "json"],
    response_schema: dict[str, Any] | None,
) -> tuple[Any | None, CliRuntimeExecutionResult | None]:
    try:
        step = _next_adapter_step(
            adapter,
            messages=messages,
            settings=settings,
            system_prompt=system_prompt,
            model_name=model_name,
            iteration=iteration,
            working_directory=working_directory,
            response_format=response_format,
            response_schema=response_schema,
        )
        return step, None
    except _PreflightFailure as exc:
        return None, _finalize_execution_result(
            context,
            status="failure",
            summary=str(exc),
            stop_reason="context_window",
            iteration=iteration,
        )
    except Exception as exc:
        logger.exception("CLI runtime adapter failed", extra={"iteration": iteration})
        return None, _finalize_execution_result(
            context,
            status="error",
            summary=f"CLI runtime adapter failed at iteration {iteration}: {exc}",
            stop_reason="adapter_error",
            iteration=iteration,
        )


def _enforce_phase_budgets(
    *,
    context: _ResultContext,
    settings: CliRuntimeSettings,
    iteration: int,
    commands_with_writes: int,
    first_execution_iteration: int | None,
    messages: list[CliRuntimeMessage],
    stall_correction_injected_at: int | None,
) -> tuple[CliRuntimeExecutionResult | None, int | None]:
    if commands_with_writes == 0:
        if (
            settings.max_exploration_iterations is not None
            and iteration > settings.max_exploration_iterations
        ):
            if stall_correction_injected_at is None and settings.stall_correction_turns > 0:
                messages.append(
                    CliRuntimeMessage(
                        role="assistant",
                        content=(
                            "Runtime corrective message: exploration budget is nearly exhausted. "
                            "Please do one now: (1) produce a concise plan, "
                            "(2) make the first concrete change, or "
                            "(3) provide a final answer with findings and gaps."
                        ),
                    )
                )
                return None, iteration
            if (
                stall_correction_injected_at is not None
                and iteration - stall_correction_injected_at <= settings.stall_correction_turns
            ):
                return None, stall_correction_injected_at
            return (
                _finalize_execution_result(
                    context,
                    status="failure",
                    summary=(
                        "CLI runtime exhausted its exploration-phase budget "
                        "before producing a plan, concrete edit, or final answer."
                    ),
                    stop_reason="exploration_exhausted",
                    iteration=iteration,
                ),
                stall_correction_injected_at,
            )
    elif first_execution_iteration is not None:
        execution_count = iteration - first_execution_iteration
        if (
            settings.max_execution_iterations is not None
            and execution_count > settings.max_execution_iterations
        ):
            return (
                _finalize_execution_result(
                    context,
                    status="failure",
                    summary=(
                        "CLI runtime exhausted its execution-phase budget "
                        f"({settings.max_execution_iterations}) before reaching a final answer."
                    ),
                    stop_reason="budget_exceeded",
                    iteration=iteration,
                ),
                stall_correction_injected_at,
            )
    return None, stall_correction_injected_at


def _run_turn_preflight_checks(
    context: _ResultContext,
    iteration: int,
    settings: CliRuntimeSettings,
    loop_state: Any,
    cancel_token: Callable[[], bool] | None,
    stall_correction_injected_at: int | None,
) -> tuple[CliRuntimeExecutionResult | None, int | None]:
    cancelled_result = _maybe_finalize_for_cancel_or_timeout(
        context=context,
        cancel_token=cancel_token,
        clock=context.clock,
        started_at=context.started_at,
        settings=settings,
        iteration=iteration,
    )
    if cancelled_result is not None:
        return cancelled_result, stall_correction_injected_at

    return _enforce_phase_budgets(
        context=context,
        settings=settings,
        iteration=iteration,
        commands_with_writes=loop_state.commands_with_writes,
        first_execution_iteration=loop_state.first_execution_iteration,
        messages=context.messages,
        stall_correction_injected_at=stall_correction_injected_at,
    )


def _execute_turn_step(
    context: _ResultContext,
    iteration: int,
    settings: CliRuntimeSettings,
    model_name: str | None,
    system_prompt: str,
    adapter: CliRuntimeAdapter,
    working_directory: Path | None,
    response_format: Literal["text", "json"],
    response_schema: dict[str, Any] | None,
    session: ShellSessionProtocol,
    resolved_tool_client: McpToolClient,
    granted_permission: ToolPermissionLevel,
    loop_state: Any,
    redactor: SecretRedactor | None,
    start_span: Callable[..., Any],
) -> CliRuntimeExecutionResult | None:
    step, err_result = _get_adapter_step_safe(
        adapter=adapter,
        context=context,
        messages=context.messages,
        settings=settings,
        system_prompt=system_prompt,
        model_name=model_name,
        iteration=iteration,
        working_directory=working_directory,
        response_format=response_format,
        response_schema=response_schema,
    )
    if err_result is not None:
        return err_result

    if step.kind == "final":  # type: ignore[union-attr]
        return _handle_final_step(
            context=context,
            messages=context.messages,
            final_output=step.final_output or "",  # type: ignore[union-attr]
            iteration=iteration,
        )

    return execute_tool_turn(
        context=context,
        adapter_iteration=iteration,
        step=step,
        session=session,
        settings=settings,
        resolved_tool_client=resolved_tool_client,
        granted_permission=granted_permission,
        started_at=context.started_at,
        clock=context.clock,
        messages=context.messages,
        commands_run=context.commands_run,
        loop_state=loop_state,
        redactor=redactor,
        start_span=start_span,
    )


@contextmanager
def _turn_span(model_name, iteration, start_span, task_id, session_id, messages):  # type: ignore[no-untyped-def]
    turn_name = f"{model_name} Turn {iteration}" if model_name else f"Turn {iteration}"
    with start_span(
        tracer_name=TRACER_NAME,
        span_name=turn_name,
        attributes=with_span_kind(SPAN_KIND_AGENT),
        task_id=task_id,
        session_id=session_id,
    ) as turn_span:
        last_msg = messages[-1].content if messages else "Task started"
        set_span_input_output(input_data=last_msg)
        set_optional_span_attribute(turn_span, "iteration", iteration)
        if model_name:
            set_optional_span_attribute(turn_span, "model", model_name)
        yield turn_span


def _run_single_turn_iteration(
    *,
    iteration: int,
    context: _ResultContext,
    settings: CliRuntimeSettings,
    model_name: str | None,
    system_prompt: str,
    adapter: CliRuntimeAdapter,
    working_directory: Path | None,
    response_format: Literal["text", "json"],
    response_schema: dict[str, Any] | None,
    session: ShellSessionProtocol,
    resolved_tool_client: McpToolClient,
    granted_permission: ToolPermissionLevel,
    loop_state: Any,
    redactor: SecretRedactor | None,
    start_span: Callable[..., Any],
    stall_correction_injected_at: int | None,
    cancel_token: Callable[[], bool] | None,
    task_id: str | None,
    session_id: str | None,
) -> tuple[CliRuntimeExecutionResult | None, int | None]:
    with _turn_span(
        model_name=model_name,
        iteration=iteration,
        start_span=start_span,
        task_id=task_id or settings.task_id,
        session_id=session_id or settings.session_id,
        messages=context.messages,
    ):
        _update_budget_ledger(
            context.budget_ledger,
            started_at=context.started_at,
            clock=context.clock,
            iterations_used=iteration - 1,
        )

        preflight_result, stall_correction_injected_at = _run_turn_preflight_checks(
            context=context,
            iteration=iteration,
            settings=settings,
            loop_state=loop_state,
            cancel_token=cancel_token,
            stall_correction_injected_at=stall_correction_injected_at,
        )
        if preflight_result is not None:
            return preflight_result, stall_correction_injected_at

        step_result = _execute_turn_step(
            context=context,
            iteration=iteration,
            settings=settings,
            model_name=model_name,
            system_prompt=system_prompt,
            adapter=adapter,
            working_directory=working_directory,
            response_format=response_format,
            response_schema=response_schema,
            session=session,
            resolved_tool_client=resolved_tool_client,
            granted_permission=granted_permission,
            loop_state=loop_state,
            redactor=redactor,
            start_span=start_span,
        )
        if step_result is not None:
            return step_result, stall_correction_injected_at

        return maybe_finalize_for_stall(
            context=context,
            settings=settings,
            iteration=iteration,
            messages=context.messages,
            commands_run=context.commands_run,
            loop_state=loop_state,
            stall_correction_injected_at=stall_correction_injected_at,
            started_at=context.started_at,
            clock=context.clock,
        )


def _next_adapter_step(  # type: ignore[no-untyped-def]
    adapter: CliRuntimeAdapter,
    *,
    messages: list[CliRuntimeMessage],
    settings: CliRuntimeSettings,
    system_prompt: str,
    model_name: str | None,
    iteration: int,
    working_directory: Path | None,
    response_format: Literal["text", "json"],
    response_schema: dict[str, Any] | None,
):
    messages_for_adapter, preflight_error = _preflight_messages_for_adapter(
        messages,
        settings=settings,
        model_name=model_name,
        iteration=iteration,
    )
    if preflight_error is not None:
        raise _PreflightFailure(preflight_error)
    return adapter.next_step(
        tuple(messages_for_adapter),
        system_prompt=system_prompt,
        working_directory=working_directory,
        task_id=settings.task_id,
        session_id=settings.session_id,
        response_format=response_format,
        response_schema=response_schema,
    )


def _handle_final_step(
    *,
    context: _ResultContext,
    messages: list[CliRuntimeMessage],
    final_output: str,
    iteration: int,
) -> CliRuntimeExecutionResult:
    stripped_output = final_output.strip()
    messages.append(CliRuntimeMessage(role="assistant", content=stripped_output))
    set_span_input_output(input_data=None, output_data=stripped_output)

    parsed_step = _parse_runtime_step_from_text(stripped_output)
    if (parsed_step is not None and parsed_step.kind == "tool_call") or (
        parsed_step is None and _looks_like_tool_call_payload_text(stripped_output)
    ):
        return _finalize_execution_result(
            context,
            status="error",
            summary=(
                "CLI runtime adapter returned a tool_call payload as final output "
                f"at iteration {iteration}."
            ),
            stop_reason="adapter_error",
            iteration=iteration,
        )
    return _finalize_execution_result(
        context,
        status="success",
        summary=stripped_output,
        stop_reason="final_answer",
        iteration=iteration,
    )
