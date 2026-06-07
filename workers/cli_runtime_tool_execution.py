"""Tool invocation helpers for the CLI runtime loop."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from apps.observability import (
    SPAN_KIND_TOOL,
    set_optional_span_attribute,
    set_span_input_output,
    set_span_status_from_outcome,
    with_span_kind,
)
from sandbox import DockerShellSessionError
from sandbox.redact import SecretRedactor, redact_and_truncate_output
from tools import (
    McpToolClient,
    ToolPermissionLevel,
    UnknownToolError,
)
from workers.base import WorkerCommand
from workers.cli_runtime_budget import (
    _finalize_execution_result,
    _resolve_command_timeout_seconds,
    _ResultContext,
    _retry_command_budget_key,
    _retry_command_key,
    _update_budget_ledger,
)
from workers.cli_runtime_context import _extract_file_hints_from_command, _looks_read_only_command
from workers.cli_runtime_tool_policy import (
    enforce_retry_and_command_limits,
    enforce_tool_permissions,
    handle_invalid_tool_input,
    handle_unknown_tool,
)
from workers.cli_runtime_tools import (
    _normalize_requested_tool_name,
    _resolve_tool_command,
    _tool_call_transcript,
    format_tool_observation,
)
from workers.cli_runtime_types import CliRuntimeExecutionResult

TRACER_NAME = "workers.cli_runtime"


ToolTurnResult: TypeAlias = CliRuntimeExecutionResult | None  # noqa: UP040


@dataclass
class ToolExecutionState:
    commands_with_writes: int = 0
    first_execution_iteration: int | None = None
    read_counts_by_file: dict[str, int] = field(default_factory=dict)
    recent_iteration_signals: list[dict[str, Any]] = field(default_factory=list)
    seen_files: set[str] = field(default_factory=set)


@dataclass
class _PreparedToolInvocation:
    tool: Any
    command: str
    command_budget_key: str
    previous_failures: int
    is_retry: bool


def execute_tool_turn(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    adapter_iteration: int,
    step,
    session,
    settings,
    resolved_tool_client: McpToolClient,
    granted_permission: ToolPermissionLevel,
    started_at: float,
    clock: Callable[[], float],
    messages,
    commands_run: list[WorkerCommand],
    loop_state: ToolExecutionState,
    redactor: SecretRedactor | None,
    start_span: Callable[..., Any],
):
    prepared, execution_result = _prepare_tool_invocation(
        context=context,
        adapter_iteration=adapter_iteration,
        step=step,
        settings=settings,
        resolved_tool_client=resolved_tool_client,
        granted_permission=granted_permission,
        messages=messages,
    )
    if execution_result is not None:
        return execution_result
    if prepared is None:
        return None

    _record_tool_call(context=context, prepared=prepared, messages=messages)
    shell_result, execution_result = _execute_tool_command(
        context=context,
        adapter_iteration=adapter_iteration,
        session=session,
        settings=settings,
        prepared=prepared,
        started_at=started_at,
        clock=clock,
        redactor=redactor,
        start_span=start_span,
    )
    if execution_result is not None:
        return execution_result
    assert shell_result is not None

    _record_tool_result(
        context=context,
        settings=settings,
        adapter_iteration=adapter_iteration,
        prepared=prepared,
        shell_result=shell_result,
        commands_run=commands_run,
        loop_state=loop_state,
        started_at=started_at,
        clock=clock,
        messages=messages,
        redactor=redactor,
    )

    return None


def _prepare_tool_invocation(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    adapter_iteration: int,
    step,
    settings,
    resolved_tool_client: McpToolClient,
    granted_permission: ToolPermissionLevel,
    messages,
):
    assert step.tool_name is not None
    requested_tool_name = _normalize_requested_tool_name(step.tool_name)

    try:
        tool = resolved_tool_client.require_tool_definition(requested_tool_name)
    except UnknownToolError as exc:
        return None, handle_unknown_tool(
            context=context,
            settings=settings,
            requested_tool_name=requested_tool_name,
            exc=exc,
            iteration=adapter_iteration,
            messages=messages,
        )

    assert step.tool_input is not None
    try:
        command = _resolve_tool_command(tool, step.tool_input)
    except ValueError as exc:
        return None, handle_invalid_tool_input(
            context=context,
            settings=settings,
            tool_name=tool.name,
            tool_input=step.tool_input,
            error=str(exc),
            iteration=adapter_iteration,
            messages=messages,
        )

    execution_result = enforce_tool_permissions(
        context=context,
        settings=settings,
        tool=tool,
        command=command,
        granted_permission=granted_permission,
        iteration=adapter_iteration,
        read_only=settings.read_only,
    )
    if execution_result is not None:
        return None, execution_result

    command_key = _retry_command_key(command)
    command_budget_key = _retry_command_budget_key(command_key)
    previous_failures = context.budget_ledger.failed_command_attempts.get(command_budget_key, 0)
    is_retry = previous_failures > 0

    execution_result = enforce_retry_and_command_limits(
        context=context,
        settings=settings,
        tool_name=tool.name,
        command=command,
        iteration=adapter_iteration,
        is_retry=is_retry,
        previous_failures=previous_failures,
    )
    if execution_result is not None:
        return None, execution_result

    return (
        _PreparedToolInvocation(
            tool=tool,
            command=command,
            command_budget_key=command_budget_key,
            previous_failures=previous_failures,
            is_retry=is_retry,
        ),
        None,
    )


def _record_tool_call(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    prepared: _PreparedToolInvocation,
    messages,
) -> None:
    context.budget_ledger.tool_calls_used += 1
    if prepared.is_retry:
        context.budget_ledger.retries_used += 1
    messages.append(
        type(messages[0])(
            role="assistant",
            content=_tool_call_transcript(prepared.tool, prepared.command),
        )
    )


def _execute_tool_command(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    adapter_iteration: int,
    session,
    settings,
    prepared: _PreparedToolInvocation,
    started_at: float,
    clock: Callable[[], float],
    redactor: SecretRedactor | None,
    start_span: Callable[..., Any],
):
    with start_span(
        tracer_name=TRACER_NAME,
        span_name=f"tool.{prepared.tool.name}",
        attributes=with_span_kind(SPAN_KIND_TOOL),
        task_id=settings.task_id,
        session_id=settings.session_id,
    ) as span:
        set_optional_span_attribute(span, "tool.name", prepared.tool.name)
        set_optional_span_attribute(span, "tool.input", prepared.command)
        set_span_input_output(input_data=prepared.command)
        try:
            shell_result = session.execute(
                prepared.command,
                timeout_seconds=_resolve_command_timeout_seconds(
                    tool=prepared.tool,
                    started_at=started_at,
                    settings=settings,
                    clock=clock,
                ),
            )
        except DockerShellSessionError as exc:
            return None, _finalize_execution_result(
                context,
                status="error",
                summary=(
                    f"CLI runtime failed while executing `{prepared.tool.name}` "
                    f"at iteration {adapter_iteration}: {exc}"
                ),
                stop_reason="shell_error",
                iteration=adapter_iteration,
            )

        set_span_input_output(
            input_data=None,
            output_data=redact_and_truncate_output(
                shell_result.output,
                redactor=redactor,
                limit_chars=settings.max_observation_characters,
            ),
        )
        if shell_result.exit_code == 0:
            set_span_status_from_outcome("success")
        else:
            set_span_status_from_outcome(
                "failure",
                f"Command failed with exit code {shell_result.exit_code}",
            )

    return shell_result, None


def _update_loop_state_from_command(  # type: ignore[no-untyped-def]
    *,
    settings,
    adapter_iteration: int,
    command: str,
    loop_state: ToolExecutionState,
) -> None:
    read_only_command = _looks_read_only_command(command)
    if not read_only_command:
        loop_state.commands_with_writes += 1
        if loop_state.first_execution_iteration is None:
            loop_state.first_execution_iteration = adapter_iteration
        loop_state.read_counts_by_file.clear()

    file_hints = _extract_file_hints_from_command(command)
    new_file_hints_count = 0
    for file_hint in file_hints:
        if file_hint not in loop_state.seen_files:
            loop_state.seen_files.add(file_hint)
            new_file_hints_count += 1

    if read_only_command:
        for file_hint in set(file_hints):
            loop_state.read_counts_by_file[file_hint] = (
                loop_state.read_counts_by_file.get(file_hint, 0) + 1
            )

    loop_state.recent_iteration_signals.append(
        {"read_only": read_only_command, "files": file_hints, "new_files": new_file_hints_count}
    )
    if len(loop_state.recent_iteration_signals) > settings.stall_window_iterations:
        loop_state.recent_iteration_signals = loop_state.recent_iteration_signals[
            -settings.stall_window_iterations :
        ]


def _record_tool_result(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    settings,
    adapter_iteration: int,
    prepared: _PreparedToolInvocation,
    shell_result,
    commands_run: list[WorkerCommand],
    loop_state: ToolExecutionState,
    started_at: float,
    clock: Callable[[], float],
    messages,
    redactor: SecretRedactor | None,
):
    context.budget_ledger.shell_commands_used += 1
    commands_run.append(
        WorkerCommand(
            command=prepared.command,
            exit_code=shell_result.exit_code,
            duration_seconds=shell_result.duration_seconds,
        )
    )

    _update_loop_state_from_command(
        settings=settings,
        adapter_iteration=adapter_iteration,
        command=prepared.command,
        loop_state=loop_state,
    )

    if shell_result.exit_code == 0:
        context.budget_ledger.failed_command_attempts.pop(prepared.command_budget_key, None)
    else:
        context.budget_ledger.failed_command_attempts[prepared.command_budget_key] = (
            prepared.previous_failures + 1
        )

    _update_budget_ledger(
        context.budget_ledger,
        started_at=started_at,
        clock=clock,
        iterations_used=adapter_iteration,
    )
    messages.append(
        type(messages[0])(
            role="tool",
            tool_name=prepared.tool.name,
            content=format_tool_observation(
                shell_result,
                tool_name=prepared.tool.name,
                max_characters=settings.max_observation_characters,
                redactor=redactor,
            ),
        )
    )
