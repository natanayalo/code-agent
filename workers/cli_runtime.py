"""Shared CLI runtime helpers for iterative coding workers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sandbox import DockerShellCommandResult, DockerShellSessionError
from workers.base import WorkerCommand

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_WORKER_TIMEOUT_SECONDS = 300
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
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
    tool_name: Literal["execute_bash"] | None = None
    tool_input: str | None = None
    final_output: str | None = None

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> CliRuntimeStep:
        if self.kind == "tool_call":
            if self.tool_name != "execute_bash":
                raise ValueError("Tool calls must target execute_bash.")
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
    max_observation_characters: int = Field(
        default=DEFAULT_MAX_OBSERVATION_CHARACTERS,
        ge=256,
    )


class CliRuntimeExecutionResult(CliRuntimeModel):
    """Structured outcome of one CLI runtime execution loop."""

    status: Literal["success", "failure", "error"]
    summary: str = Field(min_length=1)
    stop_reason: Literal[
        "final_answer",
        "max_iterations",
        "worker_timeout",
        "shell_error",
        "adapter_error",
    ]
    commands_run: list[WorkerCommand] = Field(default_factory=list)
    messages: list[CliRuntimeMessage] = Field(default_factory=list)


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
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        integer_value = int(value)
        return integer_value if integer_value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = int(float(stripped))
        except (OverflowError, ValueError):
            return None
        return parsed if parsed > 0 else None
    return None


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

    max_observation_characters = _coerce_positive_int(budget.get("max_observation_characters"))
    if max_observation_characters is not None:
        resolved["max_observation_characters"] = max_observation_characters

    return CliRuntimeSettings.model_validate(resolved)


def _truncate_text(text: str, *, max_characters: int) -> tuple[str, bool]:
    """Return bounded observation text and whether truncation occurred."""
    if len(text) <= max_characters:
        return text, False
    return text[:max_characters].rstrip(), True


def _tool_call_transcript(command: str) -> str:
    """Render a compact assistant transcript entry for a bash tool call."""
    return "\n".join(
        [
            "Tool call: execute_bash",
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


def _split_git_status_rename_candidate(candidate: str) -> tuple[str, str] | None:
    """Split a porcelain rename entry on the last unquoted rename separator."""
    separator = " -> "
    in_quotes = False
    escaped = False
    separator_index = -1

    for index, character in enumerate(candidate):
        if escaped:
            escaped = False
            continue
        if character == "\\" and in_quotes:
            escaped = True
            continue
        if character == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and candidate.startswith(separator, index):
            separator_index = index

    if separator_index == -1:
        return None
    return (
        candidate[:separator_index],
        candidate[separator_index + len(separator) :],
    )


def _remaining_command_timeout_seconds(
    *,
    started_at: float,
    settings: CliRuntimeSettings,
    clock: Callable[[], float],
) -> int:
    """Clamp per-command timeout by the overall worker timeout budget."""
    elapsed_seconds = clock() - started_at
    remaining_seconds = max(int(settings.worker_timeout_seconds - elapsed_seconds), 1)
    return min(settings.command_timeout_seconds, remaining_seconds)


def run_cli_runtime_loop(
    adapter: CliRuntimeAdapter,
    session: ShellSessionProtocol,
    *,
    system_prompt: str,
    settings: CliRuntimeSettings,
    clock: Callable[[], float] = perf_counter,
) -> CliRuntimeExecutionResult:
    """Drive the provider adapter through a bounded multi-turn shell loop."""
    started_at = clock()
    messages = [CliRuntimeMessage(role="system", content=system_prompt)]
    commands_run: list[WorkerCommand] = []

    for iteration in range(1, settings.max_iterations + 1):
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
            )

        try:
            step = adapter.next_step(tuple(messages))
        except Exception as exc:
            logger.exception("CLI runtime adapter failed", extra={"iteration": iteration})
            return CliRuntimeExecutionResult(
                status="error",
                summary=f"CLI runtime adapter failed at iteration {iteration}: {exc}",
                stop_reason="adapter_error",
                commands_run=commands_run,
                messages=messages,
            )

        if step.kind == "final":
            assert step.final_output is not None  # Validated by CliRuntimeStep.
            final_output = step.final_output.strip()
            messages.append(CliRuntimeMessage(role="assistant", content=final_output))
            return CliRuntimeExecutionResult(
                status="success",
                summary=final_output,
                stop_reason="final_answer",
                commands_run=commands_run,
                messages=messages,
            )

        assert step.tool_input is not None  # Validated by CliRuntimeStep.
        command = step.tool_input.strip()
        messages.append(CliRuntimeMessage(role="assistant", content=_tool_call_transcript(command)))

        try:
            shell_result = session.execute(
                command,
                timeout_seconds=_remaining_command_timeout_seconds(
                    started_at=started_at,
                    settings=settings,
                    clock=clock,
                ),
            )
        except DockerShellSessionError as exc:
            return CliRuntimeExecutionResult(
                status="error",
                summary=f"CLI runtime failed while executing bash at iteration {iteration}: {exc}",
                stop_reason="shell_error",
                commands_run=commands_run,
                messages=messages,
            )

        commands_run.append(
            WorkerCommand(
                command=command,
                exit_code=shell_result.exit_code,
                duration_seconds=shell_result.duration_seconds,
            )
        )
        messages.append(
            CliRuntimeMessage(
                role="tool",
                tool_name="execute_bash",
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
    )


def collect_changed_files(
    session: ShellSessionProtocol,
    *,
    timeout_seconds: int = DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS,
) -> list[str]:
    """Collect changed paths from the git workspace when available."""
    try:
        status_result = session.execute(
            "git status --short --untracked-files=all",
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
    for line in status_result.output.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        candidate = line[3:].strip()
        if "R" in status and " -> " in candidate:
            rename_parts = _split_git_status_rename_candidate(candidate)
            if rename_parts is not None:
                _, candidate = rename_parts
                candidate = candidate.strip()
        if candidate.startswith('"') and candidate.endswith('"') and len(candidate) >= 2:
            candidate = candidate[1:-1]
        if candidate:
            changed_files.append(candidate)

    return list(dict.fromkeys(changed_files))
