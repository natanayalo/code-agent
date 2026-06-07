"""Boundary models and runtime settings for the shared CLI runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sandbox import DockerShellCommandResult
from tools import ToolPermissionDecision
from tools.numeric import coerce_non_negative_int_like, coerce_positive_int_like
from workers.base import WorkerCommand
from workers.constants import (
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_CONTEXT_CONDENSER_RECENT_MESSAGES,
    DEFAULT_CONTEXT_CONDENSER_SUMMARY_MAX_CHARACTERS,
    DEFAULT_CONTEXT_CONDENSER_THRESHOLD_CHARACTERS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_OBSERVATION_CHARACTERS,
    DEFAULT_MAX_REPEATED_FILE_READS,
    DEFAULT_STALL_CORRECTION_TURNS,
    DEFAULT_STALL_WINDOW_ITERATIONS,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
)


class CliRuntimeModel(BaseModel):
    """Base model for CLI runtime boundary types."""

    model_config = ConfigDict(extra="forbid")


class CliRuntimeMessage(CliRuntimeModel):
    """One message exchanged inside the shared CLI runtime loop."""

    role: Literal["system", "assistant", "tool"]
    content: str = Field(min_length=1)
    tool_name: str | None = None

    @model_validator(mode="after")
    def _validate_role_specific_fields(self) -> CliRuntimeMessage:
        if self.role == "tool" and not self.tool_name:
            raise ValueError("Tool messages must include tool_name.")
        if self.role != "tool" and self.tool_name is not None:
            raise ValueError("Only tool messages may set tool_name.")
        return self


class CliRuntimeStep(CliRuntimeModel):
    """One adapter decision in the CLI runtime loop."""

    kind: Literal["tool_call", "final"]
    tool_name: str | None = None
    tool_input: str | None = None
    final_output: str | None = None

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> CliRuntimeStep:
        if self.kind == "tool_call":
            if self.tool_name is None or not self.tool_name.strip():
                raise ValueError("Tool calls must target a registered tool name.")
            if self.tool_input is None or not self.tool_input.strip():
                raise ValueError("Tool calls must include a non-empty tool_input.")
            if self.final_output is not None:
                raise ValueError("Tool calls cannot include final_output.")
            return self

        if self.final_output is None or not self.final_output.strip():
            raise ValueError("Final runtime steps must include final_output.")
        if self.tool_name is not None or self.tool_input is not None:
            raise ValueError("Final runtime steps cannot include tool call fields.")
        return self


class CliRuntimeSettings(CliRuntimeModel):
    """Inner-loop safety settings for a CLI runtime worker."""

    task_id: str | None = None
    session_id: str | None = None
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1)
    worker_timeout_seconds: int = Field(default=DEFAULT_WORKER_TIMEOUT_SECONDS, ge=1)
    command_timeout_seconds: int = Field(default=DEFAULT_COMMAND_TIMEOUT_SECONDS, ge=1)
    read_only: bool = False
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_shell_commands: int | None = Field(default=None, ge=0)
    max_retries: int | None = Field(default=None, ge=0)
    max_verifier_passes: int | None = Field(default=None, ge=0)
    max_exploration_iterations: int | None = Field(default=None, ge=1)
    max_execution_iterations: int | None = Field(default=None, ge=1)
    stall_window_iterations: int = Field(default=DEFAULT_STALL_WINDOW_ITERATIONS, ge=2)
    max_repeated_file_reads: int = Field(default=DEFAULT_MAX_REPEATED_FILE_READS, ge=2)
    stall_correction_turns: int = Field(default=DEFAULT_STALL_CORRECTION_TURNS, ge=0)
    max_observation_characters: int = Field(
        default=DEFAULT_MAX_OBSERVATION_CHARACTERS,
        ge=256,
    )
    context_condenser_threshold_characters: int | None = Field(
        default=DEFAULT_CONTEXT_CONDENSER_THRESHOLD_CHARACTERS,
        ge=1024,
    )
    context_condenser_recent_messages: int = Field(
        default=DEFAULT_CONTEXT_CONDENSER_RECENT_MESSAGES,
        ge=2,
    )
    context_condenser_summary_max_characters: int = Field(
        default=DEFAULT_CONTEXT_CONDENSER_SUMMARY_MAX_CHARACTERS,
        ge=256,
    )
    context_window_limit_tokens: int | None = Field(
        default=None,
        ge=1,
    )


class CliRuntimeBudgetLedger(CliRuntimeModel):
    """Best-effort runtime budget usage and limit tracking."""

    max_iterations: int = Field(ge=1)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_shell_commands: int | None = Field(default=None, ge=0)
    max_retries: int | None = Field(default=None, ge=0)
    max_verifier_passes: int | None = Field(default=None, ge=0)
    iterations_used: int = Field(default=0, ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    shell_commands_used: int = Field(default=0, ge=0)
    retries_used: int = Field(default=0, ge=0)
    verifier_passes_used: int = Field(default=0, ge=0)
    failed_command_attempts: dict[str, int] = Field(default_factory=dict)
    wall_clock_seconds: float = Field(default=0.0, ge=0)


class CliRuntimeExecutionResult(CliRuntimeModel):
    """Structured outcome of one CLI runtime execution loop."""

    status: Literal["success", "failure", "error"]
    summary: str = Field(min_length=1)
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
    ]
    commands_run: list[WorkerCommand] = Field(default_factory=list)
    messages: list[CliRuntimeMessage] = Field(default_factory=list)
    budget_ledger: CliRuntimeBudgetLedger
    permission_decision: ToolPermissionDecision | None = None


class CliRuntimeAdapter(Protocol):
    """Provider-specific adapter used by the shared CLI runtime loop."""

    def next_step(
        self,
        messages: Sequence[CliRuntimeMessage],
        *,
        system_prompt: str | None = None,
        prompt_override: str | None = None,
        working_directory: Path | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        response_format: Literal["text", "json"] = "text",
        response_schema: dict[str, Any] | None = None,
    ) -> CliRuntimeStep:
        """Return the next tool call or final answer."""


class ShellSessionProtocol(Protocol):
    """Minimal shell-session interface used by the CLI runtime."""

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        """Execute one shell command inside the persistent workspace session."""

    def close(self) -> None:
        """Close the shell session and release resources."""


def _coerce_non_negative_int(value: object) -> int | None:
    """Compatibility wrapper around shared non-negative coercion helper."""
    return coerce_non_negative_int_like(value)


def _apply_budget_overrides(resolved: dict[str, Any], budget: Mapping[str, Any]) -> None:
    for key in (
        "max_iterations",
        "max_exploration_iterations",
        "max_execution_iterations",
        "stall_window_iterations",
        "max_repeated_file_reads",
        "max_observation_characters",
        "context_condenser_threshold_characters",
        "context_condenser_recent_messages",
        "context_condenser_summary_max_characters",
        "context_window_limit_tokens",
    ):
        val = coerce_positive_int_like(budget.get(key))
        if val is not None:
            resolved[key] = val

    for key in (
        "max_tool_calls",
        "max_shell_commands",
        "max_retries",
        "max_verifier_passes",
        "stall_correction_turns",
    ):
        val = _coerce_non_negative_int(budget.get(key))
        if val is not None:
            resolved[key] = val

    worker_timeout = coerce_positive_int_like(budget.get("worker_timeout_seconds"))
    if worker_timeout is None:
        max_minutes = coerce_positive_int_like(budget.get("max_minutes"))
        if max_minutes is not None:
            worker_timeout = max_minutes * 60
    if worker_timeout is not None:
        resolved["worker_timeout_seconds"] = worker_timeout

    command_timeout = coerce_positive_int_like(budget.get("command_timeout_seconds"))
    if command_timeout is not None:
        resolved["command_timeout_seconds"] = command_timeout


def settings_from_budget(
    budget: Mapping[str, Any],
    *,
    defaults: CliRuntimeSettings | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    read_only: bool = False,
) -> CliRuntimeSettings:
    """Merge supported runtime safety overrides from a worker request budget."""
    resolved = (defaults or CliRuntimeSettings()).model_dump()
    if task_id:
        resolved["task_id"] = task_id
    if session_id:
        resolved["session_id"] = session_id
    if read_only:
        resolved["read_only"] = True

    _apply_budget_overrides(resolved, budget)

    return CliRuntimeSettings.model_validate(resolved)
