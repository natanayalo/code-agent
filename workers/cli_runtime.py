"""Shared CLI runtime helpers for iterative coding workers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from apps.observability import start_optional_span
from sandbox.redact import SecretRedactor
from tools import (
    DEFAULT_MCP_TOOL_CLIENT,
    McpToolClient,
    ToolPermissionLevel,
    ToolRegistry,
)
from workers.cli_runtime_context import (
    _build_condensed_context_summary,
    _estimate_messages_characters,
    _extract_file_hints_from_command,
    _looks_read_only_command,
    _messages_for_adapter_turn,
)
from workers.cli_runtime_files import (
    collect_changed_files,
    collect_changed_files_from_repo_path,
    collect_changed_files_since_ref_from_repo_path,
)
from workers.cli_runtime_loop import run_cli_runtime_loop_impl
from workers.cli_runtime_tools import (
    _normalize_requested_tool_name,
    format_bash_observation,
    format_tool_observation,
)
from workers.cli_runtime_types import (
    CliRuntimeAdapter,
    CliRuntimeBudgetLedger,
    CliRuntimeExecutionResult,
    CliRuntimeMessage,
    CliRuntimeSettings,
    CliRuntimeStep,
    ShellSessionProtocol,
    _coerce_non_negative_int,
    settings_from_budget,
)

logger = logging.getLogger(__name__)


def run_cli_runtime_loop(
    adapter: CliRuntimeAdapter,
    session: ShellSessionProtocol,
    *,
    system_prompt: str,
    settings: CliRuntimeSettings,
    tool_registry: ToolRegistry | None = None,
    tool_client: McpToolClient | None = None,
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
) -> CliRuntimeExecutionResult:
    """Drive the provider adapter through a bounded multi-turn shell loop."""
    return run_cli_runtime_loop_impl(
        adapter,
        session,
        system_prompt=system_prompt,
        settings=settings,
        tool_registry=tool_registry,
        tool_client=tool_client
        or (DEFAULT_MCP_TOOL_CLIENT if tool_registry is None else tool_registry.mcp_client),
        granted_permission=granted_permission,
        clock=clock,
        working_directory=working_directory,
        cancel_token=cancel_token,
        task_id=task_id,
        session_id=session_id,
        model_name=model_name,
        redactor=redactor,
        response_format=response_format,
        response_schema=response_schema,
        start_span=start_optional_span,
    )


__all__ = [
    "CliRuntimeAdapter",
    "CliRuntimeBudgetLedger",
    "CliRuntimeExecutionResult",
    "CliRuntimeMessage",
    "CliRuntimeSettings",
    "CliRuntimeStep",
    "ShellSessionProtocol",
    "_build_condensed_context_summary",
    "_coerce_non_negative_int",
    "_estimate_messages_characters",
    "_extract_file_hints_from_command",
    "_looks_read_only_command",
    "_messages_for_adapter_turn",
    "_normalize_requested_tool_name",
    "collect_changed_files",
    "collect_changed_files_from_repo_path",
    "collect_changed_files_since_ref_from_repo_path",
    "format_bash_observation",
    "format_tool_observation",
    "run_cli_runtime_loop",
    "settings_from_budget",
]
