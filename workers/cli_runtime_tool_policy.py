"""Policy and validation helpers for CLI runtime tool calls."""

from __future__ import annotations

from tools import (
    STR_REPLACE_EDITOR_TOOL_NAME,
    ToolPermissionDecision,
    ToolPermissionLevel,
    resolve_bash_command_permission,
)
from workers.cli_runtime_budget import _finalize_execution_result, _ResultContext
from workers.cli_runtime_context import _looks_read_only_command
from workers.cli_runtime_tools import (
    _format_invalid_tool_input_observation,
    _format_unsupported_tool_observation,
)

_RECOVERABLE_UNKNOWN_TOOL_NAMES = frozenset({"enter_plan_mode", "exit_plan_mode"})


def handle_unknown_tool(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    settings,
    requested_tool_name: str,
    exc: Exception,
    iteration: int,
    messages,
):
    if requested_tool_name in _RECOVERABLE_UNKNOWN_TOOL_NAMES:
        if (
            settings.max_tool_calls is not None
            and context.budget_ledger.tool_calls_used >= settings.max_tool_calls
        ):
            return _finalize_execution_result(
                context,
                status="failure",
                summary=(
                    "CLI runtime exceeded its tool-call budget "
                    f"({settings.max_tool_calls}) before handling `{requested_tool_name}`."
                ),
                stop_reason="budget_exceeded",
                iteration=iteration,
            )
        context.budget_ledger.tool_calls_used += 1
        messages.append(
            type(messages[0])(
                role="tool",
                tool_name=requested_tool_name,
                content=_format_unsupported_tool_observation(
                    tool_name=requested_tool_name,
                    max_characters=settings.max_observation_characters,
                ),
            )
        )
        return None

    return _finalize_execution_result(
        context,
        status="error",
        summary=f"CLI runtime adapter requested an unknown tool: {exc}",
        stop_reason="adapter_error",
        iteration=iteration,
    )


def handle_invalid_tool_input(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    settings,
    tool_name: str,
    tool_input: str,
    error: str,
    iteration: int,
    messages,
):
    if (
        settings.max_tool_calls is not None
        and context.budget_ledger.tool_calls_used >= settings.max_tool_calls
    ):
        return _finalize_execution_result(
            context,
            status="failure",
            summary=(
                "CLI runtime exceeded its tool-call budget "
                f"({settings.max_tool_calls}) before handling `{tool_name}` input."
            ),
            stop_reason="budget_exceeded",
            iteration=iteration,
        )
    context.budget_ledger.tool_calls_used += 1
    messages.append(
        type(messages[0])(
            role="tool",
            tool_name=tool_name,
            content=_format_invalid_tool_input_observation(
                tool_name=tool_name,
                tool_input=tool_input,
                error=error,
                max_characters=settings.max_observation_characters,
            ),
        )
    )
    if tool_name == STR_REPLACE_EDITOR_TOOL_NAME:
        return None
    return _finalize_execution_result(
        context,
        status="error",
        summary=f"CLI runtime adapter provided invalid input for `{tool_name}`: {error}",
        stop_reason="adapter_error",
        iteration=iteration,
    )


def enforce_tool_permissions(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    settings,
    tool,
    command: str,
    granted_permission: ToolPermissionLevel,
    iteration: int,
    read_only: bool,
):
    if read_only and not _looks_read_only_command(command):
        permission_decision = ToolPermissionDecision(
            tool_name=tool.name,
            command=command,
            granted_permission=ToolPermissionLevel.READ_ONLY,
            required_permission=ToolPermissionLevel.WORKSPACE_WRITE,
            allowed=False,
            reason="Command blocked by read-only enforcement policy.",
        )
    else:
        permission_decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=granted_permission,
        )

    if permission_decision.allowed:
        return None

    updated_context = _ResultContext(
        started_at=context.started_at,
        clock=context.clock,
        budget_ledger=context.budget_ledger,
        commands_run=context.commands_run,
        messages=context.messages,
        permission_decision=permission_decision,
    )
    return _finalize_execution_result(
        updated_context,
        status="failure",
        summary=(
            "CLI runtime needs higher permission before executing "
            f"`{tool.name}`. Required: {permission_decision.required_permission.value}; "
            f"granted: {permission_decision.granted_permission.value}. "
            f"{permission_decision.reason}"
        ),
        stop_reason="permission_required",
        iteration=iteration,
    )


def enforce_retry_and_command_limits(  # type: ignore[no-untyped-def]
    *,
    context: _ResultContext,
    settings,
    tool_name: str,
    command: str,
    iteration: int,
    is_retry: bool,
    previous_failures: int,
):
    if (
        settings.max_tool_calls is not None
        and context.budget_ledger.tool_calls_used >= settings.max_tool_calls
    ):
        return _finalize_execution_result(
            context,
            status="failure",
            summary=(
                "CLI runtime exceeded its tool-call budget "
                f"({settings.max_tool_calls}) before executing `{tool_name}`."
            ),
            stop_reason="budget_exceeded",
            iteration=iteration,
        )
    if (
        settings.max_shell_commands is not None
        and context.budget_ledger.shell_commands_used >= settings.max_shell_commands
    ):
        return _finalize_execution_result(
            context,
            status="failure",
            summary=(
                "CLI runtime exceeded its shell-command budget "
                f"({settings.max_shell_commands}) before executing `{command}`."
            ),
            stop_reason="budget_exceeded",
            iteration=iteration,
        )
    if settings.max_retries is not None and is_retry and previous_failures > settings.max_retries:
        return _finalize_execution_result(
            context,
            status="failure",
            summary=(
                "CLI runtime exceeded its retry budget "
                f"({settings.max_retries}) while retrying `{command}`."
            ),
            stop_reason="budget_exceeded",
            iteration=iteration,
        )
    return None
