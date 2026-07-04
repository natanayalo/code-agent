# ruff: noqa: F401
"""Shared fixtures and helpers for CLI runtime tests."""

from __future__ import annotations

import subprocess
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Literal

from sandbox import DockerShellCommandResult, DockerShellSessionError
from tools import (
    DEFAULT_EXECUTE_BROWSER_TIMEOUT_SECONDS,
    DEFAULT_TOOL_REGISTRY,
    McpToolClient,
    ToolPermissionLevel,
    ToolRegistry,
    build_str_replace_editor_command_from_input,
    build_view_file_command_from_input,
)
from workers.cli_runtime import (
    CliRuntimeBudgetLedger,
    CliRuntimeMessage,
    CliRuntimeSettings,
    CliRuntimeStep,
    _build_condensed_context_summary,
    _coerce_non_negative_int,
    _estimate_messages_characters,
    _extract_file_hints_from_command,
    _looks_read_only_command,
    _messages_for_adapter_turn,
    _normalize_requested_tool_name,
    collect_changed_files,
    collect_changed_files_from_repo_path,
    collect_changed_files_since_ref_from_repo_path,
    format_bash_observation,
    run_cli_runtime_loop,
    settings_from_budget,
)


class _ScriptedAdapter:
    def __init__(self, steps: list[CliRuntimeStep]) -> None:
        self._steps = list(steps)
        self.calls: list[list[CliRuntimeMessage]] = []

    def next_step(
        self,
        messages: list[CliRuntimeMessage],
        *,
        system_prompt: str | None = None,
        prompt_override: str | None = None,
        working_directory: Path | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        response_format: Literal["text", "json"] = "text",
        response_schema: dict[str, Any] | None = None,
    ) -> CliRuntimeStep:
        self.calls.append(list(messages))
        if not self._steps:
            raise AssertionError("Adapter received more turns than expected.")
        return self._steps.pop(0)


class _FakeSession:
    def __init__(self, responses: dict[str, DockerShellCommandResult | Exception]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        self.calls.append((command, timeout_seconds))
        response = self._responses[command]
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _command_result(command: str, *, output: str, exit_code: int = 0) -> DockerShellCommandResult:
    return DockerShellCommandResult(
        command=command,
        output=output,
        exit_code=exit_code,
        duration_seconds=0.25,
    )


__all__ = [
    "_ScriptedAdapter",
    "_FakeSession",
    "_command_result",
    *[
        name
        for name in globals()
        if not name.startswith("__")
        and name not in {"_ScriptedAdapter", "_FakeSession", "_command_result"}
    ],
]
