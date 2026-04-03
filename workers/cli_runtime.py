"""Shared CLI runtime helpers for iterative coding workers."""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable, Mapping, Sequence
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sandbox import DockerShellCommandResult, DockerShellSessionError
from tools import (
    DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS,
    DEFAULT_TOOL_REGISTRY,
    ToolDefinition,
    ToolPermissionDecision,
    ToolPermissionLevel,
    ToolRegistry,
    UnknownToolError,
    resolve_bash_command_permission,
)
from workers.base import WorkerCommand

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_WORKER_TIMEOUT_SECONDS = 300
DEFAULT_COMMAND_TIMEOUT_SECONDS = DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS
DEFAULT_MAX_OBSERVATION_CHARACTERS = 4000
DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS = 10


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

    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1)
    worker_timeout_seconds: int = Field(default=DEFAULT_WORKER_TIMEOUT_SECONDS, ge=1)
    command_timeout_seconds: int = Field(default=DEFAULT_COMMAND_TIMEOUT_SECONDS, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_shell_commands: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0)
    max_verifier_passes: int | None = Field(default=None, ge=0)
    max_observation_characters: int = Field(
        default=DEFAULT_MAX_OBSERVATION_CHARACTERS,
        ge=256,
    )


class CliRuntimeBudgetLedger(CliRuntimeModel):
    """Best-effort runtime budget usage and limit tracking."""

    max_iterations: int = Field(ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    max_shell_commands: int | None = Field(default=None, ge=1)
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
        "worker_timeout",
        "budget_exceeded",
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

    def next_step(self, messages: Sequence[CliRuntimeMessage]) -> CliRuntimeStep:
        """Return the next tool call or final answer."""


class ShellSessionProtocol(Protocol):
    """Minimal shell-session interface used by the CLI runtime."""

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        """Execute one shell command inside the persistent workspace session."""

    def close(self) -> None:
        """Close the shell session and release resources."""


def _coerce_positive_int(value: object) -> int | None:
    """Return a positive integer override when one is present."""
    parsed = _coerce_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _coerce_int(value: object) -> int | None:
    """Parse integer-like inputs with the same truncation rules used by runtime budgets."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(float(stripped))
        except (OverflowError, ValueError):
            return None
    return None


def _coerce_non_negative_int(value: object) -> int | None:
    """Return a non-negative integer override when one is present."""
    parsed = _coerce_int(value)
    return parsed if parsed is not None and parsed >= 0 else None


def settings_from_budget(
    budget: Mapping[str, Any],
    *,
    defaults: CliRuntimeSettings | None = None,
) -> CliRuntimeSettings:
    """Merge supported runtime safety overrides from a worker request budget."""
    resolved = (defaults or CliRuntimeSettings()).model_dump()

    max_iterations = _coerce_positive_int(budget.get("max_iterations"))
    if max_iterations is not None:
        resolved["max_iterations"] = max_iterations

    worker_timeout = _coerce_positive_int(budget.get("worker_timeout_seconds"))
    if worker_timeout is None:
        max_minutes = _coerce_positive_int(budget.get("max_minutes"))
        if max_minutes is not None:
            worker_timeout = max_minutes * 60
    if worker_timeout is not None:
        resolved["worker_timeout_seconds"] = worker_timeout

    command_timeout = _coerce_positive_int(budget.get("command_timeout_seconds"))
    if command_timeout is not None:
        resolved["command_timeout_seconds"] = command_timeout

    max_tool_calls = _coerce_positive_int(budget.get("max_tool_calls"))
    if max_tool_calls is not None:
        resolved["max_tool_calls"] = max_tool_calls

    max_shell_commands = _coerce_positive_int(budget.get("max_shell_commands"))
    if max_shell_commands is not None:
        resolved["max_shell_commands"] = max_shell_commands

    max_retries = _coerce_non_negative_int(budget.get("max_retries"))
    if max_retries is not None:
        resolved["max_retries"] = max_retries

    max_verifier_passes = _coerce_non_negative_int(budget.get("max_verifier_passes"))
    if max_verifier_passes is not None:
        resolved["max_verifier_passes"] = max_verifier_passes

    max_observation_characters = _coerce_positive_int(budget.get("max_observation_characters"))
    if max_observation_characters is not None:
        resolved["max_observation_characters"] = max_observation_characters

    return CliRuntimeSettings.model_validate(resolved)


def _truncate_text(text: str, *, max_characters: int) -> tuple[str, bool]:
    """Return bounded observation text and whether truncation occurred."""
    if len(text) <= max_characters:
        return text, False
    return text[:max_characters].rstrip(), True


def _format_expected_artifacts(tool: ToolDefinition) -> str:
    """Render expected tool artifacts for prompt/runtime transcripts."""
    if not tool.expected_artifacts:
        return "none"
    return ", ".join(artifact.value for artifact in tool.expected_artifacts)


def _tool_call_transcript(tool: ToolDefinition, command: str) -> str:
    """Render a compact assistant transcript entry for a tool call."""
    return "\n".join(
        [
            f"Tool call: {tool.name}",
            f"Required permission: {tool.required_permission.value}",
            f"Default timeout seconds: {tool.timeout_seconds}",
            f"Expected artifacts: {_format_expected_artifacts(tool)}",
            "```bash",
            command,
            "```",
        ]
    )


def format_bash_observation(
    result: DockerShellCommandResult,
    *,
    max_characters: int,
) -> str:
    """Render bounded shell output for adapter follow-up turns."""
    output, truncated = _truncate_text(result.output, max_characters=max_characters)
    lines = [
        "Tool result: execute_bash",
        f"Command: {result.command}",
        f"Exit code: {result.exit_code}",
        f"Duration seconds: {result.duration_seconds:.3f}",
        "Output:",
        "```text",
        output or "<no output>",
        "```",
    ]
    if truncated:
        lines.append(f"[output truncated to {max_characters} characters]")
    return "\n".join(lines)


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


def _budget_exceeded_result(
    *,
    summary: str,
    started_at: float,
    clock: Callable[[], float],
    iteration: int,
    budget_ledger: CliRuntimeBudgetLedger,
    commands_run: list[WorkerCommand],
    messages: list[CliRuntimeMessage],
) -> CliRuntimeExecutionResult:
    """Build a consistent runtime result for budget-limit failures."""
    _update_budget_ledger(
        budget_ledger,
        started_at=started_at,
        clock=clock,
        iterations_used=iteration,
    )
    return CliRuntimeExecutionResult(
        status="failure",
        summary=summary,
        stop_reason="budget_exceeded",
        commands_run=commands_run,
        messages=messages,
        budget_ledger=budget_ledger,
    )


def run_cli_runtime_loop(
    adapter: CliRuntimeAdapter,
    session: ShellSessionProtocol,
    *,
    system_prompt: str,
    settings: CliRuntimeSettings,
    tool_registry: ToolRegistry | None = None,
    granted_permission: ToolPermissionLevel = ToolPermissionLevel.WORKSPACE_WRITE,
    clock: Callable[[], float] = perf_counter,
) -> CliRuntimeExecutionResult:
    """Drive the provider adapter through a bounded multi-turn shell loop."""
    started_at = clock()
    resolved_registry = tool_registry or DEFAULT_TOOL_REGISTRY
    messages = [CliRuntimeMessage(role="system", content=system_prompt)]
    commands_run: list[WorkerCommand] = []
    budget_ledger = _build_budget_ledger(settings)

    for iteration in range(1, settings.max_iterations + 1):
        _update_budget_ledger(
            budget_ledger,
            started_at=started_at,
            clock=clock,
            iterations_used=iteration - 1,
        )
        if clock() - started_at >= settings.worker_timeout_seconds:
            return CliRuntimeExecutionResult(
                status="failure",
                summary=(
                    "CLI runtime exceeded its worker timeout "
                    f"({settings.worker_timeout_seconds}s) before reaching a final answer."
                ),
                stop_reason="worker_timeout",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=budget_ledger,
            )

        try:
            step = adapter.next_step(tuple(messages))
        except Exception as exc:
            logger.exception("CLI runtime adapter failed", extra={"iteration": iteration})
            _update_budget_ledger(
                budget_ledger,
                started_at=started_at,
                clock=clock,
                iterations_used=iteration,
            )
            return CliRuntimeExecutionResult(
                status="error",
                summary=f"CLI runtime adapter failed at iteration {iteration}: {exc}",
                stop_reason="adapter_error",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=budget_ledger,
            )

        if step.kind == "final":
            assert step.final_output is not None  # Validated by CliRuntimeStep.
            final_output = step.final_output.strip()
            messages.append(CliRuntimeMessage(role="assistant", content=final_output))
            _update_budget_ledger(
                budget_ledger,
                started_at=started_at,
                clock=clock,
                iterations_used=iteration,
            )
            return CliRuntimeExecutionResult(
                status="success",
                summary=final_output,
                stop_reason="final_answer",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=budget_ledger,
            )

        assert step.tool_name is not None  # Validated by CliRuntimeStep.
        try:
            tool = resolved_registry.require_tool(step.tool_name)
        except UnknownToolError as exc:
            _update_budget_ledger(
                budget_ledger,
                started_at=started_at,
                clock=clock,
                iterations_used=iteration,
            )
            return CliRuntimeExecutionResult(
                status="error",
                summary=f"CLI runtime adapter requested an unknown tool: {exc}",
                stop_reason="adapter_error",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=budget_ledger,
            )

        assert step.tool_input is not None  # Validated by CliRuntimeStep.
        command = step.tool_input.strip()
        permission_decision = resolve_bash_command_permission(
            command,
            tool,
            granted_permission=granted_permission,
        )
        if not permission_decision.allowed:
            _update_budget_ledger(
                budget_ledger,
                started_at=started_at,
                clock=clock,
                iterations_used=iteration,
            )
            return CliRuntimeExecutionResult(
                status="failure",
                summary=(
                    "CLI runtime needs higher permission before executing "
                    f"`{tool.name}`. Required: {permission_decision.required_permission.value}; "
                    f"granted: {permission_decision.granted_permission.value}. "
                    f"{permission_decision.reason}"
                ),
                stop_reason="permission_required",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=budget_ledger,
                permission_decision=permission_decision,
            )

        command_key = _retry_command_key(command)
        command_budget_key = _retry_command_budget_key(command_key)
        previous_failures = budget_ledger.failed_command_attempts.get(command_budget_key, 0)
        is_retry = previous_failures > 0
        if (
            settings.max_tool_calls is not None
            and budget_ledger.tool_calls_used >= settings.max_tool_calls
        ):
            return _budget_exceeded_result(
                summary=(
                    "CLI runtime exceeded its tool-call budget "
                    f"({settings.max_tool_calls}) before executing `{tool.name}`."
                ),
                started_at=started_at,
                clock=clock,
                iteration=iteration,
                budget_ledger=budget_ledger,
                commands_run=commands_run,
                messages=messages,
            )
        if (
            settings.max_shell_commands is not None
            and budget_ledger.shell_commands_used >= settings.max_shell_commands
        ):
            return _budget_exceeded_result(
                summary=(
                    "CLI runtime exceeded its shell-command budget "
                    f"({settings.max_shell_commands}) before executing `{command}`."
                ),
                started_at=started_at,
                clock=clock,
                iteration=iteration,
                budget_ledger=budget_ledger,
                commands_run=commands_run,
                messages=messages,
            )
        if (
            settings.max_retries is not None
            and is_retry
            and previous_failures > settings.max_retries
        ):
            return _budget_exceeded_result(
                summary=(
                    "CLI runtime exceeded its retry budget "
                    f"({settings.max_retries}) while retrying `{command}`."
                ),
                started_at=started_at,
                clock=clock,
                iteration=iteration,
                budget_ledger=budget_ledger,
                commands_run=commands_run,
                messages=messages,
            )

        budget_ledger.tool_calls_used += 1
        if is_retry:
            budget_ledger.retries_used += 1
        messages.append(
            CliRuntimeMessage(role="assistant", content=_tool_call_transcript(tool, command))
        )

        try:
            shell_result = session.execute(
                command,
                timeout_seconds=_resolve_command_timeout_seconds(
                    tool=tool,
                    started_at=started_at,
                    settings=settings,
                    clock=clock,
                ),
            )
        except DockerShellSessionError as exc:
            _update_budget_ledger(
                budget_ledger,
                started_at=started_at,
                clock=clock,
                iterations_used=iteration,
            )
            return CliRuntimeExecutionResult(
                status="error",
                summary=f"CLI runtime failed while executing bash at iteration {iteration}: {exc}",
                stop_reason="shell_error",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=budget_ledger,
            )

        budget_ledger.shell_commands_used += 1
        commands_run.append(
            WorkerCommand(
                command=command,
                exit_code=shell_result.exit_code,
                duration_seconds=shell_result.duration_seconds,
            )
        )
        if shell_result.exit_code == 0:
            budget_ledger.failed_command_attempts.pop(command_budget_key, None)
        else:
            budget_ledger.failed_command_attempts[command_budget_key] = previous_failures + 1
        _update_budget_ledger(
            budget_ledger,
            started_at=started_at,
            clock=clock,
            iterations_used=iteration,
        )
        messages.append(
            CliRuntimeMessage(
                role="tool",
                tool_name=tool.name,
                content=format_bash_observation(
                    shell_result,
                    max_characters=settings.max_observation_characters,
                ),
            )
        )

    return CliRuntimeExecutionResult(
        status="failure",
        summary=(
            "CLI runtime hit its max iteration budget "
            f"({settings.max_iterations}) before reaching a final answer."
        ),
        stop_reason="max_iterations",
        commands_run=commands_run,
        messages=messages,
        budget_ledger=budget_ledger,
    )


def collect_changed_files(
    session: ShellSessionProtocol,
    *,
    timeout_seconds: int = DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS,
) -> list[str]:
    """Collect changed paths from the git workspace when available."""
    try:
        status_result = session.execute(
            "git status --porcelain=v1 -z --untracked-files=all",
            timeout_seconds=timeout_seconds,
        )
    except DockerShellSessionError:
        logger.warning("CLI runtime failed to collect changed files from git status.")
        return []

    if status_result.exit_code != 0:
        logger.warning(
            "CLI runtime could not collect changed files because git status failed.",
            extra={"exit_code": status_result.exit_code},
        )
        return []

    changed_files: list[str] = []
    items = iter(status_result.output.split("\0"))
    for item in items:
        if len(item) < 4:
            continue
        status = item[:2]
        path = item[3:]
        if "R" in status or "C" in status:
            next(items, None)
        if path:
            changed_files.append(path)

    return list(dict.fromkeys(changed_files))
