"""Shared CLI runtime helpers for iterative coding workers."""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sandbox import DockerShellCommandResult, DockerShellSessionError
from tools import (
    DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS,
    DEFAULT_MCP_TOOL_CLIENT,
    EXECUTE_BROWSER_TOOL_NAME,
    EXECUTE_GIT_TOOL_NAME,
    EXECUTE_GITHUB_TOOL_NAME,
    SEARCH_DIR_TOOL_NAME,
    SEARCH_FILE_TOOL_NAME,
    STR_REPLACE_EDITOR_TOOL_NAME,
    VIEW_FILE_TOOL_NAME,
    McpToolClient,
    ToolDefinition,
    ToolPermissionDecision,
    ToolPermissionLevel,
    ToolRegistry,
    UnknownToolError,
    build_browser_command_from_input,
    build_git_command_from_input,
    build_github_command_from_input,
    build_search_dir_command_from_input,
    build_search_file_command_from_input,
    build_str_replace_editor_command_from_input,
    build_view_file_command_from_input,
    resolve_bash_command_permission,
)
from tools.numeric import (
    coerce_non_negative_int_like,
    coerce_positive_int_like,
)
from workers.base import WorkerCommand

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_WORKER_TIMEOUT_SECONDS = 300
DEFAULT_COMMAND_TIMEOUT_SECONDS = DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS
DEFAULT_MAX_OBSERVATION_CHARACTERS = 4000
DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS = 10


def _git_status_unavailable(output: str) -> bool:
    """Return True when git status failed because the target is not a usable repo."""
    normalized = output.lower()
    return any(
        marker in normalized
        for marker in (
            "not a git repository",
            "detected dubious ownership",
            "safe.directory",
        )
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

    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1)
    worker_timeout_seconds: int = Field(default=DEFAULT_WORKER_TIMEOUT_SECONDS, ge=1)
    command_timeout_seconds: int = Field(default=DEFAULT_COMMAND_TIMEOUT_SECONDS, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_shell_commands: int | None = Field(default=None, ge=0)
    max_retries: int | None = Field(default=None, ge=0)
    max_verifier_passes: int | None = Field(default=None, ge=0)
    max_observation_characters: int = Field(
        default=DEFAULT_MAX_OBSERVATION_CHARACTERS,
        ge=256,
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

    def next_step(
        self,
        messages: Sequence[CliRuntimeMessage],
        *,
        working_directory: Path | None = None,
    ) -> CliRuntimeStep:
        """Return the next tool call or final answer."""


class ShellSessionProtocol(Protocol):
    """Minimal shell-session interface used by the CLI runtime."""

    def execute(self, command: str, *, timeout_seconds: int = 300) -> DockerShellCommandResult:
        """Execute one shell command inside the persistent workspace session."""

    def close(self) -> None:
        """Close the shell session and release resources."""


def _coerce_non_negative_int(value: object) -> int | None:
    """Compatibility wrapper used by tests around shared numeric coercion."""
    return coerce_non_negative_int_like(value)


def settings_from_budget(
    budget: Mapping[str, Any],
    *,
    defaults: CliRuntimeSettings | None = None,
) -> CliRuntimeSettings:
    """Merge supported runtime safety overrides from a worker request budget."""
    resolved = (defaults or CliRuntimeSettings()).model_dump()

    max_iterations = coerce_positive_int_like(budget.get("max_iterations"))
    if max_iterations is not None:
        resolved["max_iterations"] = max_iterations

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

    max_tool_calls = _coerce_non_negative_int(budget.get("max_tool_calls"))
    if max_tool_calls is not None:
        resolved["max_tool_calls"] = max_tool_calls

    max_shell_commands = _coerce_non_negative_int(budget.get("max_shell_commands"))
    if max_shell_commands is not None:
        resolved["max_shell_commands"] = max_shell_commands

    max_retries = _coerce_non_negative_int(budget.get("max_retries"))
    if max_retries is not None:
        resolved["max_retries"] = max_retries

    max_verifier_passes = _coerce_non_negative_int(budget.get("max_verifier_passes"))
    if max_verifier_passes is not None:
        resolved["max_verifier_passes"] = max_verifier_passes

    max_observation_characters = coerce_positive_int_like(budget.get("max_observation_characters"))
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


def format_tool_observation(
    result: DockerShellCommandResult,
    *,
    tool_name: str,
    max_characters: int,
) -> str:
    """Render bounded shell output for adapter follow-up turns."""
    output, truncated = _truncate_text(result.output, max_characters=max_characters)
    lines = [
        f"Tool result: {tool_name}",
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


def format_bash_observation(
    result: DockerShellCommandResult,
    *,
    max_characters: int,
) -> str:
    """Backward-compatible wrapper for bash observations."""
    return format_tool_observation(
        result,
        tool_name="execute_bash",
        max_characters=max_characters,
    )


def _resolve_tool_command(tool: ToolDefinition, raw_input: str) -> str:
    """Normalize tool input into the concrete shell command executed in the sandbox."""
    command = raw_input.strip()
    if tool.name == VIEW_FILE_TOOL_NAME:
        return build_view_file_command_from_input(command)
    if tool.name == SEARCH_FILE_TOOL_NAME:
        return build_search_file_command_from_input(command)
    if tool.name == SEARCH_DIR_TOOL_NAME:
        return build_search_dir_command_from_input(command)
    if tool.name == STR_REPLACE_EDITOR_TOOL_NAME:
        return build_str_replace_editor_command_from_input(command)
    if tool.name == EXECUTE_BROWSER_TOOL_NAME:
        return build_browser_command_from_input(
            command,
            timeout_seconds=tool.timeout_seconds,
        )
    if tool.name == EXECUTE_GIT_TOOL_NAME:
        return build_git_command_from_input(command)
    if tool.name == EXECUTE_GITHUB_TOOL_NAME:
        return build_github_command_from_input(command)
    return command


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
    tool_client: McpToolClient | None = None,
    granted_permission: ToolPermissionLevel = ToolPermissionLevel.WORKSPACE_WRITE,
    clock: Callable[[], float] = perf_counter,
    working_directory: Path | None = None,
    cancel_token: Callable[[], bool] | None = None,
) -> CliRuntimeExecutionResult:
    """Drive the provider adapter through a bounded multi-turn shell loop."""
    started_at = clock()
    resolved_tool_client = tool_client or (
        DEFAULT_MCP_TOOL_CLIENT if tool_registry is None else tool_registry.mcp_client
    )
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

        if cancel_token is not None and cancel_token():
            return CliRuntimeExecutionResult(
                status="error",
                summary="CLI runtime loop was cancelled by the orchestrator timeout.",
                stop_reason="worker_timeout",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=budget_ledger,
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
            step = adapter.next_step(
                tuple(messages),
                working_directory=working_directory,
            )
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
            tool = resolved_tool_client.require_tool_definition(step.tool_name)
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
        try:
            command = _resolve_tool_command(tool, step.tool_input)
        except ValueError as exc:
            _update_budget_ledger(
                budget_ledger,
                started_at=started_at,
                clock=clock,
                iterations_used=iteration,
            )
            return CliRuntimeExecutionResult(
                status="error",
                summary=f"CLI runtime adapter provided invalid input for `{tool.name}`: {exc}",
                stop_reason="adapter_error",
                commands_run=commands_run,
                messages=messages,
                budget_ledger=budget_ledger,
            )
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
                summary=(
                    f"CLI runtime failed while executing `{tool.name}` "
                    f"at iteration {iteration}: {exc}"
                ),
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
                content=format_tool_observation(
                    shell_result,
                    tool_name=tool.name,
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
    working_directory: Path | None = None,
    timeout_seconds: int = DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS,
) -> list[str]:
    """Collect changed paths from the git workspace when available."""
    changed_files: list[str] = []
    git_command_prefix = (
        f"git -C {shlex.quote(str(working_directory))}" if working_directory is not None else "git"
    )
    porcelain_z_command = f"{git_command_prefix} status --porcelain=v1 -z --untracked-files=all"
    fallback_command = f"{git_command_prefix} status --porcelain=v1 --untracked-files=all"

    def _parse_porcelain_z(output: str) -> list[str]:
        parsed: list[str] = []
        items = iter(output.split("\0"))
        for item in items:
            if len(item) < 4:
                continue
            status = item[:2]
            path = item[3:]
            if "R" in status or "C" in status:
                next(items, None)
            if path:
                parsed.append(path)
        return parsed

    def _parse_porcelain_lines(output: str) -> list[str]:
        parsed: list[str] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            status = line[:2]
            path = line[3:]
            if not path:
                continue
            if ("R" in status or "C" in status) and " -> " in path:
                _, path = path.split(" -> ", 1)
            parsed.append(path)
        return parsed

    try:
        status_result = session.execute(porcelain_z_command, timeout_seconds=timeout_seconds)
    except DockerShellSessionError:
        logger.warning(
            "CLI runtime failed to collect changed files from git status with porcelain -z; "
            "falling back to line-delimited output."
        )
        status_result = None

    if status_result is not None and status_result.exit_code == 0:
        changed_files.extend(_parse_porcelain_z(status_result.output))
        return list(dict.fromkeys(changed_files))

    if status_result is not None and status_result.exit_code != 0:
        if _git_status_unavailable(status_result.output):
            logger.info(
                "CLI runtime skipped changed-file collection because workspace is not a "
                "usable git repository.",
                extra={"exit_code": status_result.exit_code},
            )
            return []
        logger.warning(
            "CLI runtime could not collect changed files with porcelain -z because "
            "git status failed; "
            "falling back to line-delimited output.",
            extra={"exit_code": status_result.exit_code},
        )

    try:
        fallback_result = session.execute(fallback_command, timeout_seconds=timeout_seconds)
    except DockerShellSessionError:
        logger.warning(
            "CLI runtime failed to collect changed files from fallback git status output."
        )
        return []

    if fallback_result.exit_code != 0:
        if _git_status_unavailable(fallback_result.output):
            logger.info(
                "CLI runtime skipped changed-file fallback because workspace is not a "
                "usable git repository.",
                extra={"exit_code": fallback_result.exit_code},
            )
            return []
        logger.warning(
            "CLI runtime could not collect changed files because fallback git status failed.",
            extra={"exit_code": fallback_result.exit_code},
        )
        return []

    changed_files.extend(_parse_porcelain_lines(fallback_result.output))

    return list(dict.fromkeys(changed_files))


def collect_changed_files_from_repo_path(
    repo_path: Path,
    *,
    timeout_seconds: int = DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS,
) -> list[str]:
    """Collect changed paths by running git status directly on the repo path."""
    command = [
        "git",
        "-C",
        str(repo_path),
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            "Worker git status timed out while collecting changed files via host fallback.",
            extra={"timeout_seconds": timeout_seconds},
            exc_info=exc,
        )
        return []
    except OSError as exc:
        logger.warning(
            "Worker failed to collect changed files via host git status.",
            exc_info=exc,
        )
        return []

    if completed.returncode != 0:
        output = (completed.stdout or b"").decode("utf-8", errors="replace")
        if _git_status_unavailable(output):
            logger.info(
                "Worker skipped host-side changed-file collection because workspace is not a "
                "usable git repository.",
                extra={"exit_code": completed.returncode},
            )
            return []
        logger.warning(
            "Worker could not collect changed files via host git status.",
            extra={"exit_code": completed.returncode},
        )
        return []

    changed_files: list[str] = []
    items = iter(completed.stdout.decode("utf-8", errors="replace").split("\0"))
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
