"""Shared CLI runtime helpers for iterative coding workers."""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.observability import (
    SPAN_KIND_AGENT,
    SPAN_KIND_TOOL,
    STATUS_ERROR,
    STATUS_OK,
    set_optional_span_attribute,
    set_span_input_output,
    set_span_status,
    set_span_status_from_outcome,
    start_optional_span,
    with_span_kind,
)
from sandbox import DockerShellCommandResult, DockerShellSessionError
from sandbox.redact import (
    SecretRedactor,
    mask_url_credentials,
    redact_and_truncate_output,
    sanitize_command,
)
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
from workers.adapter_utils import truncate_detail_keep_tail
from workers.base import WorkerCommand

logger = logging.getLogger(__name__)

TRACER_NAME: Final[str] = "workers.cli_runtime"

DEFAULT_MAX_ITERATIONS = 8
DEFAULT_WORKER_TIMEOUT_SECONDS = 600
DEFAULT_COMMAND_TIMEOUT_SECONDS = DEFAULT_EXECUTE_BASH_TIMEOUT_SECONDS
DEFAULT_MAX_OBSERVATION_CHARACTERS = 4000
DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS = 10
DEFAULT_CONTEXT_CONDENSER_THRESHOLD_CHARACTERS = 12000
DEFAULT_CONTEXT_CONDENSER_RECENT_MESSAGES = 6
DEFAULT_CONTEXT_CONDENSER_SUMMARY_MAX_CHARACTERS = 1500
DEFAULT_CONTEXT_WINDOW_WARNING_RATIO = 0.8
DEFAULT_ESTIMATED_CHARACTERS_PER_TOKEN = 4
DEFAULT_CONDENSED_SUMMARY_MAX_DECISIONS = 5
DEFAULT_CONDENSED_SUMMARY_MAX_FILE_HINTS = 8
DEFAULT_CONDENSED_SUMMARY_MAX_ERRORS = 3
DEFAULT_STALL_WINDOW_ITERATIONS = 3
DEFAULT_MAX_REPEATED_FILE_READS = 3
DEFAULT_STALL_CORRECTION_TURNS = 1
MODEL_CONTEXT_WINDOW_TOKENS: dict[str, int] = {
    "gpt-5.4": 272000,
    "gemini-2.5-pro": 1048576,
}
_TOOL_NAME_ALIASES: dict[str, str] = {
    "functions.exec_command": "execute_bash",
    "exec_command": "execute_bash",
    "bash": "execute_bash",
    "run_shell_command": "execute_bash",
}
_RECOVERABLE_UNKNOWN_TOOL_NAMES = frozenset({"enter_plan_mode", "exit_plan_mode"})
_READ_ONLY_COMMAND_PREFIXES = (
    "awk ",
    "cat ",
    "find ",
    "git diff",
    "git log",
    "git show",
    "git status",
    "grep ",
    "head ",
    "less ",
    "ls",
    "more ",
    "pwd",
    "rg ",
    "sed -n",
    "tail ",
    "wc ",
)
_WRITE_COMMAND_MARKERS = (
    ">",
    " >",
    ">>",
    "chmod ",
    "chown ",
    "cp ",
    "git add ",
    "git apply ",
    "git commit",
    "git mv ",
    "git restore ",
    "git rm ",
    "mkdir ",
    "mv ",
    "patch ",
    "rm ",
    "rmdir ",
    "sed -i",
    "tee ",
    "touch ",
)
_FILE_ARGUMENT_COMMANDS = frozenset(
    {
        "awk",
        "bash",
        "cat",
        "chmod",
        "chown",
        "cp",
        "git",
        "grep",
        "head",
        "less",
        "ln",
        "ls",
        "mkdir",
        "more",
        "mv",
        "python",
        "python3",
        "rm",
        "rmdir",
        "sed",
        "sh",
        "tail",
        "tee",
        "touch",
        "wc",
    }
)
_COMMANDS_WITH_LEADING_NON_PATH_ARGUMENT = frozenset({"awk", "chmod", "chown", "grep", "sed"})
_GIT_FILE_ARGUMENT_SUBCOMMANDS = frozenset({"add", "mv", "restore", "rm"})


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

    task_id: str | None = None
    session_id: str | None = None
    max_iterations: int = Field(default=DEFAULT_MAX_ITERATIONS, ge=1)
    worker_timeout_seconds: int = Field(default=DEFAULT_WORKER_TIMEOUT_SECONDS, ge=1)
    command_timeout_seconds: int = Field(default=DEFAULT_COMMAND_TIMEOUT_SECONDS, ge=1)
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


def settings_from_budget(
    budget: Mapping[str, Any],
    *,
    defaults: CliRuntimeSettings | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
) -> CliRuntimeSettings:
    """Merge supported runtime safety overrides from a worker request budget."""
    resolved = (defaults or CliRuntimeSettings()).model_dump()
    if task_id:
        resolved["task_id"] = task_id
    if session_id:
        resolved["session_id"] = session_id

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
    max_exploration_iterations = coerce_positive_int_like(budget.get("max_exploration_iterations"))
    if max_exploration_iterations is not None:
        resolved["max_exploration_iterations"] = max_exploration_iterations

    max_execution_iterations = coerce_positive_int_like(budget.get("max_execution_iterations"))
    if max_execution_iterations is not None:
        resolved["max_execution_iterations"] = max_execution_iterations

    stall_window_iterations = coerce_positive_int_like(budget.get("stall_window_iterations"))
    if stall_window_iterations is not None:
        resolved["stall_window_iterations"] = stall_window_iterations

    max_repeated_file_reads = coerce_positive_int_like(budget.get("max_repeated_file_reads"))
    if max_repeated_file_reads is not None:
        resolved["max_repeated_file_reads"] = max_repeated_file_reads

    stall_correction_turns = _coerce_non_negative_int(budget.get("stall_correction_turns"))
    if stall_correction_turns is not None:
        resolved["stall_correction_turns"] = stall_correction_turns

    max_observation_characters = coerce_positive_int_like(budget.get("max_observation_characters"))
    if max_observation_characters is not None:
        resolved["max_observation_characters"] = max_observation_characters

    context_condenser_threshold = coerce_positive_int_like(
        budget.get("context_condenser_threshold_characters")
    )
    if context_condenser_threshold is not None:
        resolved["context_condenser_threshold_characters"] = context_condenser_threshold

    context_condenser_recent_messages = coerce_positive_int_like(
        budget.get("context_condenser_recent_messages")
    )
    if context_condenser_recent_messages is not None:
        resolved["context_condenser_recent_messages"] = context_condenser_recent_messages

    context_condenser_summary_max_characters = coerce_positive_int_like(
        budget.get("context_condenser_summary_max_characters")
    )
    if context_condenser_summary_max_characters is not None:
        resolved["context_condenser_summary_max_characters"] = (
            context_condenser_summary_max_characters
        )

    context_window_limit_tokens = coerce_positive_int_like(
        budget.get("context_window_limit_tokens")
    )
    if context_window_limit_tokens is not None:
        resolved["context_window_limit_tokens"] = context_window_limit_tokens

    return CliRuntimeSettings.model_validate(resolved)


def _truncate_text(text: str, *, max_characters: int) -> tuple[str, bool]:
    """Return bounded observation text and whether truncation occurred."""
    if len(text) <= max_characters:
        return text, False
    return text[:max_characters].rstrip(), True


def _estimate_messages_characters(messages: Sequence[CliRuntimeMessage]) -> int:
    """Approximate transcript size used for context-condensation checks."""
    return sum(
        len(message.role)
        + len(message.content)
        + (len(message.tool_name) if message.tool_name is not None else 0)
        for message in messages
    )


def _estimate_messages_tokens(
    messages: Sequence[CliRuntimeMessage],
    *,
    characters_per_token: int = DEFAULT_ESTIMATED_CHARACTERS_PER_TOKEN,
) -> int:
    """Approximate transcript token count from character length."""
    if not messages:
        return 0
    total_characters = _estimate_messages_characters(messages)
    return max((total_characters + characters_per_token - 1) // characters_per_token, 1)


def _resolve_context_window_limit_tokens(
    *,
    settings: CliRuntimeSettings,
    model_name: str | None,
) -> int | None:
    """Resolve model context limit from explicit settings or the model registry."""
    if settings.context_window_limit_tokens is not None:
        return settings.context_window_limit_tokens
    if model_name is None:
        return None
    normalized_model = model_name.strip().lower()
    if not normalized_model:
        return None
    return MODEL_CONTEXT_WINDOW_TOKENS.get(normalized_model)


def _log_context_window_warning(
    *,
    iteration: int,
    estimated_tokens: int,
    limit_tokens: int,
    model_name: str | None,
) -> None:
    """Emit an early warning when prompt usage approaches the context limit."""
    logger.warning(
        "CLI runtime prompt size crossed context-window warning threshold",
        extra={
            "iteration": iteration,
            "estimated_prompt_tokens": estimated_tokens,
            "context_window_limit_tokens": limit_tokens,
            "model_name": model_name,
        },
    )


def _preflight_messages_for_adapter(
    messages: list[CliRuntimeMessage],
    *,
    settings: CliRuntimeSettings,
    model_name: str | None,
    iteration: int,
) -> tuple[list[CliRuntimeMessage], str | None]:
    """Prepare messages for an adapter turn with model-context preflight checks."""
    messages_for_adapter = _messages_for_adapter_turn(messages, settings=settings)
    limit_tokens = _resolve_context_window_limit_tokens(settings=settings, model_name=model_name)
    if limit_tokens is None:
        return messages_for_adapter, None

    warning_threshold_tokens = max(int(limit_tokens * DEFAULT_CONTEXT_WINDOW_WARNING_RATIO), 1)
    estimated_tokens = _estimate_messages_tokens(messages_for_adapter)
    if estimated_tokens >= warning_threshold_tokens:
        _log_context_window_warning(
            iteration=iteration,
            estimated_tokens=estimated_tokens,
            limit_tokens=limit_tokens,
            model_name=model_name,
        )
    if estimated_tokens <= limit_tokens:
        return messages_for_adapter, None

    preflight_threshold_characters = max(
        limit_tokens * DEFAULT_ESTIMATED_CHARACTERS_PER_TOKEN,
        1024,
    )
    condensed_settings = settings.model_copy(
        update={
            "context_condenser_threshold_characters": preflight_threshold_characters,
            "context_condenser_summary_max_characters": min(
                settings.context_condenser_summary_max_characters,
                max(preflight_threshold_characters // 4, 256),
            ),
        }
    )
    messages_for_adapter = _messages_for_adapter_turn(messages, settings=condensed_settings)
    estimated_tokens = _estimate_messages_tokens(messages_for_adapter)
    if estimated_tokens >= warning_threshold_tokens:
        _log_context_window_warning(
            iteration=iteration,
            estimated_tokens=estimated_tokens,
            limit_tokens=limit_tokens,
            model_name=model_name,
        )
    if estimated_tokens <= limit_tokens:
        return messages_for_adapter, None

    model_hint = model_name or "unknown-model"
    return (
        messages_for_adapter,
        (
            "CLI runtime prompt exceeded the model context window before dispatch "
            f"(estimated {estimated_tokens} tokens; limit {limit_tokens} tokens for "
            f"{model_hint})."
        ),
    )


def _extract_command_from_code_fence(content: str) -> str | None:
    """Extract a command from the runtime's bash fenced tool-call transcript."""
    match = re.search(r"```bash[ \t]*\n(?P<command>.*?)\n```", content, flags=re.DOTALL)
    if match is None:
        return None
    command = match.group("command").strip()
    return command or None


def _extract_prefixed_line(content: str, *, prefix: str) -> str | None:
    """Find a line in `content` that starts with `prefix`."""
    for line in content.splitlines():
        if line.startswith(prefix):
            value = line.removeprefix(prefix).strip()
            if value:
                return value
    return None


def _extract_output_excerpt(content: str) -> str | None:
    """Extract the last non-empty output line from a tool observation."""
    match = re.search(r"```text[ \t]*\n(?P<output>.*?)\n```", content, flags=re.DOTALL)
    if match is None:
        return None
    lines = [line.strip() for line in match.group("output").splitlines() if line.strip()]
    return lines[-1] if lines else None


def _extract_file_hints_from_command(command: str) -> list[str]:
    """Infer likely file paths touched by a command without shell execution."""
    hints: list[str] = []
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    primary_command = ""
    command_argument_index = 0
    git_subcommand: str | None = None
    for token in tokens:
        candidate = token.strip("\"'")
        if not candidate:
            continue
        if candidate in {"&&", "||", ";", "|", "|&", "&"}:
            primary_command = ""
            command_argument_index = 0
            git_subcommand = None
            continue
        if not primary_command:
            primary_command = candidate
            command_argument_index = 0
            git_subcommand = None
            continue
        if candidate.startswith("-") or candidate in {
            "<",
            ">",
            ">>",
            "2>",
            "2>>",
            "2>&1",
            "&>",
            "&>>",
            "1>",
            "1>>",
            "|&",
            ">|",
        }:
            continue
        if candidate in {".", ".."}:
            continue
        if primary_command == "git" and git_subcommand is None:
            git_subcommand = candidate
            command_argument_index += 1
            continue
        if (
            primary_command in _COMMANDS_WITH_LEADING_NON_PATH_ARGUMENT
            and command_argument_index == 0
        ):
            command_argument_index += 1
            continue
        if "/" in candidate or "." in Path(candidate).name:
            hints.append(candidate)
            command_argument_index += 1
            continue
        if (
            primary_command in _FILE_ARGUMENT_COMMANDS
            and "=" not in candidate
            and candidate != primary_command
        ):
            if primary_command == "git" and git_subcommand not in _GIT_FILE_ARGUMENT_SUBCOMMANDS:
                command_argument_index += 1
                continue
            hints.append(candidate)
        command_argument_index += 1
    return hints


def _looks_read_only_command(command: str) -> bool:
    """Best-effort command classification for stall detection heuristics."""
    normalized = " ".join(command.strip().split()).lower()
    if not normalized:
        return True
    if any(marker in normalized for marker in _WRITE_COMMAND_MARKERS):
        return False
    return any(
        normalized == prefix.rstrip() or normalized.startswith(prefix.rstrip() + " ")
        for prefix in _READ_ONLY_COMMAND_PREFIXES
    )


def _inline_code(value: str) -> str:
    """Render inline code while safely handling content that contains backticks."""
    max_tick_run = max((len(match.group(0)) for match in re.finditer(r"`+", value)), default=0)
    fence = "`" * (max_tick_run + 1)
    space = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{space}{value}{space}{fence}"


def _build_condensed_context_summary(
    older_messages: Sequence[CliRuntimeMessage],
    *,
    max_characters: int,
) -> str:
    """Build deterministic condensed context for older loop iterations."""
    decisions: list[str] = []
    files_touched: list[str] = []
    errors: list[str] = []

    for message in older_messages:
        command = _extract_command_from_code_fence(message.content)
        if command is not None:
            decisions.append(command)
            files_touched.extend(_extract_file_hints_from_command(command))
        if message.role != "tool":
            continue
        observed_command = _extract_prefixed_line(message.content, prefix="Command: ")
        if observed_command is not None:
            files_touched.extend(_extract_file_hints_from_command(observed_command))
        raw_exit_code = _extract_prefixed_line(message.content, prefix="Exit code: ")
        if raw_exit_code is None:
            continue
        try:
            exit_code = int(raw_exit_code)
        except ValueError:
            continue
        if exit_code == 0:
            continue
        output_excerpt = _extract_output_excerpt(message.content) or "<see tool output>"
        errors.append(f"exit {exit_code} ({output_excerpt})")

    deduped_decisions = list(dict.fromkeys(reversed(decisions)))[::-1]
    deduped_files = list(dict.fromkeys(reversed(files_touched)))[::-1]
    deduped_errors = list(dict.fromkeys(reversed(errors)))[::-1]

    current_state = "no tool state available from condensed history"
    for message in reversed(older_messages):
        if message.role != "tool":
            continue
        observed_command = _extract_prefixed_line(message.content, prefix="Command: ")
        raw_exit_code = _extract_prefixed_line(message.content, prefix="Exit code: ")
        if observed_command and raw_exit_code:
            current_state = (
                f"last command {_inline_code(observed_command)} exited with code {raw_exit_code}"
            )
            break

    summary = "\n".join(
        [
            "Condensed context summary (older iterations):",
            (
                "- Key decisions made: "
                + (
                    ", ".join(
                        _inline_code(command)
                        for command in deduped_decisions[-DEFAULT_CONDENSED_SUMMARY_MAX_DECISIONS:]
                    )
                    if deduped_decisions
                    else "none"
                )
            ),
            (
                "- Files touched hints: "
                + (
                    ", ".join(
                        _inline_code(path)
                        for path in deduped_files[-DEFAULT_CONDENSED_SUMMARY_MAX_FILE_HINTS:]
                    )
                    if deduped_files
                    else "none"
                )
            ),
            (
                "- Errors encountered: "
                + (
                    ", ".join(deduped_errors[-DEFAULT_CONDENSED_SUMMARY_MAX_ERRORS:])
                    if deduped_errors
                    else "none"
                )
            ),
            f"- Current working state: {current_state}",
            "Recent raw messages follow unchanged.",
        ]
    )
    if len(summary) <= max_characters:
        return summary

    suffix = f"\n[condensed summary truncated to {max_characters} characters]"
    available_for_summary = max_characters - len(suffix)
    if available_for_summary <= 0:
        suffix_only, _ = _truncate_text(suffix, max_characters=max_characters)
        return suffix_only
    bounded_summary, _ = _truncate_text(summary, max_characters=available_for_summary)
    return f"{bounded_summary}{suffix}"


def _messages_for_adapter_turn(
    messages: list[CliRuntimeMessage],
    *,
    settings: CliRuntimeSettings,
) -> list[CliRuntimeMessage]:
    """Condense older history near budget while preserving a recent raw-message tail."""
    threshold = settings.context_condenser_threshold_characters
    if threshold is None:
        return messages
    if _estimate_messages_characters(messages) <= threshold:
        return messages
    if len(messages) <= 2:
        return messages

    system_message = messages[0] if messages[0].role == "system" else None
    non_system_messages = messages[1:] if system_message is not None else messages
    if len(non_system_messages) <= settings.context_condenser_recent_messages:
        return messages

    recent_count = min(settings.context_condenser_recent_messages, len(non_system_messages))
    older_messages = non_system_messages[:-recent_count]
    recent_messages = list(non_system_messages[-recent_count:])
    if not older_messages:
        return messages

    summary_message = CliRuntimeMessage(
        role="assistant",
        content=_build_condensed_context_summary(
            older_messages,
            max_characters=settings.context_condenser_summary_max_characters,
        ),
    )

    condensed_messages: list[CliRuntimeMessage] = []
    if system_message is not None:
        condensed_messages.append(system_message)
    condensed_messages.append(summary_message)
    condensed_messages.extend(recent_messages)

    while (
        _estimate_messages_characters(condensed_messages) > threshold and len(recent_messages) > 1
    ):
        older_messages = [*older_messages, recent_messages[0]]
        recent_messages = recent_messages[1:]
        summary_message = CliRuntimeMessage(
            role="assistant",
            content=_build_condensed_context_summary(
                older_messages,
                max_characters=settings.context_condenser_summary_max_characters,
            ),
        )
        condensed_messages = []
        if system_message is not None:
            condensed_messages.append(system_message)
        condensed_messages.append(summary_message)
        condensed_messages.extend(recent_messages)

    if _estimate_messages_characters(condensed_messages) > threshold:
        compact_summary = _build_condensed_context_summary(
            older_messages,
            max_characters=max(settings.context_condenser_summary_max_characters // 2, 256),
        )
        condensed_messages = []
        if system_message is not None:
            condensed_messages.append(system_message)
        condensed_messages.append(CliRuntimeMessage(role="assistant", content=compact_summary))
        condensed_messages.extend(recent_messages)

    return condensed_messages


def _format_expected_artifacts(tool: ToolDefinition) -> str:
    """Render expected tool artifacts for prompt/runtime transcripts."""
    if not tool.expected_artifacts:
        return "none"
    return ", ".join(artifact.value for artifact in tool.expected_artifacts)


def _normalize_requested_tool_name(tool_name: str) -> str:
    """Map known adapter/runtime aliases onto registered tool names."""
    normalized = tool_name.strip()
    if not normalized:
        return tool_name
    return _TOOL_NAME_ALIASES.get(normalized, normalized)


def _extract_first_json_object(text: str) -> str | None:
    """Extract the first syntactically valid JSON object from free-form text."""
    stripped = text.strip()
    search_from = 0
    while True:
        start = stripped.find("{", search_from)
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape_next = False
        end = -1
        for index, char in enumerate(stripped[start:], start=start):
            if escape_next:
                escape_next = False
                continue
            if in_string:
                if char == "\\":
                    escape_next = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end == -1:
            return None
        candidate = stripped[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            search_from = end + 1


def _parse_runtime_step_from_text(text: str) -> CliRuntimeStep | None:
    """Attempt to parse an embedded CliRuntimeStep payload from text content."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return CliRuntimeStep.model_validate_json(stripped)
    except Exception:
        pass
    candidate = _extract_first_json_object(stripped)
    if candidate is None:
        return None
    try:
        return CliRuntimeStep.model_validate_json(candidate)
    except Exception:
        return None


def _looks_like_tool_call_payload_text(text: str) -> bool:
    """Heuristically detect tool_call payload text even when JSON is malformed."""
    lowered = text.lower()
    return (
        '"kind"' in lowered
        and '"tool_call"' in lowered
        and '"tool_name"' in lowered
        and '"tool_input"' in lowered
    )


def _format_unsupported_tool_observation(
    *,
    tool_name: str,
    max_characters: int,
) -> str:
    """Render a recoverable observation for adapter-only control tools."""
    guidance = (
        "Tool is unavailable in this runtime. Continue with registered tools only "
        "(for example, execute_bash, view_file, search_file, search_dir, str_replace_editor)."
    )
    content, _ = _truncate_text(guidance, max_characters=max_characters)
    return "\n".join(
        [
            f"Tool result: {tool_name}",
            "Status: unavailable_tool",
            "Error: tool is not registered in this runtime.",
            f"Guidance: {content}",
        ]
    )


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
    redactor: SecretRedactor | None = None,
) -> str:
    """Render bounded shell output for adapter follow-up turns."""
    sanitized = mask_url_credentials(result.output)
    if redactor:
        sanitized = redactor.redact(sanitized)

    output = truncate_detail_keep_tail(sanitized, max_characters=max_characters)

    lines = [
        f"Tool result: {tool_name}",
        f"Command: {sanitize_command(result.command, redactor)}",
        f"Exit code: {result.exit_code}",
        f"Duration seconds: {result.duration_seconds:.3f}",
        "Output:",
        "```text",
        output if output != "<empty>" else "<no output>",
        "```",
    ]
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


def _format_invalid_tool_input_observation(
    *,
    tool_name: str,
    tool_input: str,
    error: str,
    max_characters: int,
) -> str:
    """Render recoverable tool-input validation feedback for the adapter."""
    raw_input, truncated = _truncate_text(tool_input, max_characters=max_characters)
    lines = [
        f"Tool result: {tool_name}",
        "Status: input_validation_failed",
        f"Error: {error}",
        "Raw tool_input:",
        "```text",
        raw_input or "<empty>",
        "```",
    ]
    if truncated:
        lines.append(f"[tool_input truncated to {max_characters} characters]")
    if tool_name == STR_REPLACE_EDITOR_TOOL_NAME:
        lines.append(
            "Guidance: for multiline edits, use `execute_bash` (for example, a heredoc rewrite); "
            "use `str_replace_editor` only for single-line old_text/new_text replacements."
        )
    return "\n".join(lines)


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


@dataclass(frozen=True)
class _ResultContext:
    """Encapsulates common runtime loop state for final result construction."""

    started_at: float
    clock: Callable[[], float]
    budget_ledger: CliRuntimeBudgetLedger
    commands_run: list[WorkerCommand]
    messages: list[CliRuntimeMessage]
    permission_decision: ToolPermissionDecision | None = None


def _finalize_execution_result(
    context: _ResultContext,
    *,
    status: Literal["success", "failure", "error"],
    summary: str,
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
    ],
    iteration: int,
) -> CliRuntimeExecutionResult:
    """Build a consistent runtime result and refresh the budget ledger."""
    _update_budget_ledger(
        context.budget_ledger,
        started_at=context.started_at,
        clock=context.clock,
        iterations_used=iteration,
    )
    set_span_status_from_outcome(status, summary)
    return CliRuntimeExecutionResult(
        status=status,
        summary=summary,
        stop_reason=stop_reason,
        commands_run=context.commands_run,
        messages=context.messages,
        budget_ledger=context.budget_ledger,
        permission_decision=context.permission_decision,
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
    task_id: str | None = None,
    session_id: str | None = None,
    model_name: str | None = None,
    redactor: SecretRedactor | None = None,
) -> CliRuntimeExecutionResult:
    """Drive the provider adapter through a bounded multi-turn shell loop."""
    started_at = clock()
    resolved_tool_client = tool_client or (
        DEFAULT_MCP_TOOL_CLIENT if tool_registry is None else tool_registry.mcp_client
    )
    messages = [CliRuntimeMessage(role="system", content=system_prompt)]
    commands_run: list[WorkerCommand] = []
    budget_ledger = _build_budget_ledger(settings)
    commands_with_writes = 0
    first_execution_iteration: int | None = None
    seen_files: set[str] = set()
    read_counts_by_file: dict[str, int] = {}
    recent_iteration_signals: list[dict[str, Any]] = []
    stall_correction_injected_at: int | None = None

    context = _ResultContext(
        started_at=started_at,
        clock=clock,
        budget_ledger=budget_ledger,
        commands_run=commands_run,
        messages=messages,
    )

    for iteration in range(1, settings.max_iterations + 1):
        turn_name = f"Turn {iteration}"
        if model_name:
            turn_name = f"{model_name} Turn {iteration}"

        with start_optional_span(
            tracer_name=TRACER_NAME,
            span_name=turn_name,
            attributes=with_span_kind(SPAN_KIND_AGENT),
            task_id=settings.task_id,
            session_id=settings.session_id,
        ) as turn_span:
            # Attach turn input to the current span for request/response visibility.
            last_msg_content = messages[-1].content if messages else "Task started"
            set_span_input_output(input_data=last_msg_content)
            set_optional_span_attribute(turn_span, "iteration", iteration)
            if model_name:
                set_optional_span_attribute(turn_span, "model", model_name)

            _update_budget_ledger(
                budget_ledger,
                started_at=started_at,
                clock=clock,
                iterations_used=iteration - 1,
            )

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

            if (
                settings.max_exploration_iterations is not None
                and commands_with_writes == 0
                and iteration > settings.max_exploration_iterations
            ):
                if stall_correction_injected_at is None and settings.stall_correction_turns > 0:
                    messages.append(
                        CliRuntimeMessage(
                            role="assistant",
                            content=(
                                "Runtime corrective message: exploration budget is nearly exhausted. "  # noqa: E501
                                "Please do one now: (1) produce a concise plan, "
                                "(2) make the first concrete change, or "
                                "(3) provide a final answer with findings and gaps."
                            ),
                        )
                    )
                    stall_correction_injected_at = iteration
                elif (
                    stall_correction_injected_at is not None
                    and iteration - stall_correction_injected_at <= settings.stall_correction_turns
                ):
                    pass
                else:
                    return _finalize_execution_result(
                        context,
                        status="failure",
                        summary=(
                            "CLI runtime exhausted its exploration-phase budget "
                            "before producing a plan, concrete edit, or final answer."
                        ),
                        stop_reason="exploration_exhausted",
                        iteration=iteration,
                    )
            if (
                settings.max_execution_iterations is not None
                and first_execution_iteration is not None
                and (iteration - first_execution_iteration + 1) > settings.max_execution_iterations
            ):
                return _finalize_execution_result(
                    context,
                    status="failure",
                    summary=(
                        "CLI runtime exhausted its execution-phase budget "
                        f"({settings.max_execution_iterations}) before reaching a final answer."
                    ),
                    stop_reason="budget_exceeded",
                    iteration=iteration,
                )

            try:
                messages_for_adapter, preflight_error = _preflight_messages_for_adapter(
                    messages,
                    settings=settings,
                    model_name=model_name,
                    iteration=iteration,
                )
                if preflight_error is not None:
                    return _finalize_execution_result(
                        context,
                        status="failure",
                        summary=preflight_error,
                        stop_reason="context_window",
                        iteration=iteration,
                    )
                step = adapter.next_step(
                    tuple(messages_for_adapter),
                    system_prompt=system_prompt,
                    working_directory=working_directory,
                    task_id=settings.task_id,
                    session_id=settings.session_id,
                )
            except Exception as exc:
                logger.exception("CLI runtime adapter failed", extra={"iteration": iteration})
                return _finalize_execution_result(
                    context,
                    status="error",
                    summary=f"CLI runtime adapter failed at iteration {iteration}: {exc}",
                    stop_reason="adapter_error",
                    iteration=iteration,
                )

            if step.kind == "final":
                assert step.final_output is not None  # Validated by CliRuntimeStep.
                final_output = step.final_output.strip()
                messages.append(CliRuntimeMessage(role="assistant", content=final_output))
                set_span_input_output(input_data=None, output_data=final_output)

                parsed_step = _parse_runtime_step_from_text(final_output)
                if (parsed_step is not None and parsed_step.kind == "tool_call") or (
                    parsed_step is None and _looks_like_tool_call_payload_text(final_output)
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
                    summary=final_output,
                    stop_reason="final_answer",
                    iteration=iteration,
                )

            assert step.tool_name is not None  # Validated by CliRuntimeStep.
            requested_tool_name = _normalize_requested_tool_name(step.tool_name)
            try:
                tool = resolved_tool_client.require_tool_definition(requested_tool_name)
            except UnknownToolError as exc:
                if requested_tool_name in _RECOVERABLE_UNKNOWN_TOOL_NAMES:
                    if (
                        settings.max_tool_calls is not None
                        and budget_ledger.tool_calls_used >= settings.max_tool_calls
                    ):
                        return _finalize_execution_result(
                            context,
                            status="failure",
                            summary=(
                                "CLI runtime exceeded its tool-call budget "
                                f"({settings.max_tool_calls}) before handling `{requested_tool_name}`."  # noqa: E501
                            ),
                            stop_reason="budget_exceeded",
                            iteration=iteration,
                        )
                    budget_ledger.tool_calls_used += 1
                    messages.append(
                        CliRuntimeMessage(
                            role="tool",
                            tool_name=requested_tool_name,
                            content=_format_unsupported_tool_observation(
                                tool_name=requested_tool_name,
                                max_characters=settings.max_observation_characters,
                            ),
                        )
                    )
                    continue
                return _finalize_execution_result(
                    context,
                    status="error",
                    summary=f"CLI runtime adapter requested an unknown tool: {exc}",
                    stop_reason="adapter_error",
                    iteration=iteration,
                )

            assert step.tool_input is not None  # Validated by CliRuntimeStep.
            try:
                command = _resolve_tool_command(tool, step.tool_input)
            except ValueError as exc:
                if (
                    settings.max_tool_calls is not None
                    and budget_ledger.tool_calls_used >= settings.max_tool_calls
                ):
                    return _finalize_execution_result(
                        context,
                        status="failure",
                        summary=(
                            "CLI runtime exceeded its tool-call budget "
                            f"({settings.max_tool_calls}) before handling `{tool.name}` input."
                        ),
                        stop_reason="budget_exceeded",
                        iteration=iteration,
                    )
                budget_ledger.tool_calls_used += 1
                messages.append(
                    CliRuntimeMessage(
                        role="tool",
                        tool_name=tool.name,
                        content=_format_invalid_tool_input_observation(
                            tool_name=tool.name,
                            tool_input=step.tool_input,
                            error=str(exc),
                            max_characters=settings.max_observation_characters,
                        ),
                    )
                )
                if tool.name == STR_REPLACE_EDITOR_TOOL_NAME:
                    continue
                return _finalize_execution_result(
                    context,
                    status="error",
                    summary=f"CLI runtime adapter provided invalid input for `{tool.name}`: {exc}",
                    stop_reason="adapter_error",
                    iteration=iteration,
                )
            permission_decision = resolve_bash_command_permission(
                command,
                tool,
                granted_permission=granted_permission,
            )
            if not permission_decision.allowed:
                # Update context with the latest decision before finalizing
                context = _ResultContext(
                    started_at=context.started_at,
                    clock=context.clock,
                    budget_ledger=context.budget_ledger,
                    commands_run=context.commands_run,
                    messages=context.messages,
                    permission_decision=permission_decision,
                )
                return _finalize_execution_result(
                    context,
                    status="failure",
                    summary=(
                        "CLI runtime needs higher permission before executing "
                        f"`{tool.name}`. Required: {permission_decision.required_permission.value}; "  # noqa: E501
                        f"granted: {permission_decision.granted_permission.value}. "
                        f"{permission_decision.reason}"
                    ),
                    stop_reason="permission_required",
                    iteration=iteration,
                )

            command_key = _retry_command_key(command)
            command_budget_key = _retry_command_budget_key(command_key)
            previous_failures = budget_ledger.failed_command_attempts.get(command_budget_key, 0)
            is_retry = previous_failures > 0
            if (
                settings.max_tool_calls is not None
                and budget_ledger.tool_calls_used >= settings.max_tool_calls
            ):
                return _finalize_execution_result(
                    context,
                    status="failure",
                    summary=(
                        "CLI runtime exceeded its tool-call budget "
                        f"({settings.max_tool_calls}) before executing `{tool.name}`."
                    ),
                    stop_reason="budget_exceeded",
                    iteration=iteration,
                )
            if (
                settings.max_shell_commands is not None
                and budget_ledger.shell_commands_used >= settings.max_shell_commands
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
            if (
                settings.max_retries is not None
                and is_retry
                and previous_failures > settings.max_retries
            ):
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

            budget_ledger.tool_calls_used += 1
            if is_retry:
                budget_ledger.retries_used += 1
            messages.append(
                CliRuntimeMessage(role="assistant", content=_tool_call_transcript(tool, command))
            )

            with start_optional_span(
                tracer_name=TRACER_NAME,
                span_name=f"tool.{tool.name}",
                attributes=with_span_kind(SPAN_KIND_TOOL),
                task_id=settings.task_id,
                session_id=settings.session_id,
            ) as span:
                set_optional_span_attribute(span, "tool.name", tool.name)
                set_optional_span_attribute(span, "tool.input", command)
                set_span_input_output(input_data=command)
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
                    set_span_input_output(
                        input_data=None,
                        output_data=redact_and_truncate_output(
                            shell_result.output,
                            redactor=redactor,
                            limit_chars=settings.max_observation_characters,
                        ),
                    )
                    if shell_result.exit_code == 0:
                        set_span_status(STATUS_OK)
                    else:
                        set_span_status(
                            STATUS_ERROR, f"Command failed with exit code {shell_result.exit_code}"
                        )
                except DockerShellSessionError as exc:
                    return _finalize_execution_result(
                        context,
                        status="error",
                        summary=(
                            f"CLI runtime failed while executing `{tool.name}` "
                            f"at iteration {iteration}: {exc}"
                        ),
                        stop_reason="shell_error",
                        iteration=iteration,
                    )

                budget_ledger.shell_commands_used += 1
                commands_run.append(
                    WorkerCommand(
                        command=command,
                        exit_code=shell_result.exit_code,
                        duration_seconds=shell_result.duration_seconds,
                    )
                )
                read_only_command = _looks_read_only_command(command)
                if not read_only_command:
                    commands_with_writes += 1
                    if first_execution_iteration is None:
                        first_execution_iteration = iteration
                    read_counts_by_file = {}
                file_hints = _extract_file_hints_from_command(command)
                new_file_hints_count = 0
                for file_hint in file_hints:
                    if file_hint not in seen_files:
                        seen_files.add(file_hint)
                        new_file_hints_count += 1
                if read_only_command:
                    for file_hint in set(file_hints):
                        read_counts_by_file[file_hint] = read_counts_by_file.get(file_hint, 0) + 1
                recent_iteration_signals.append(
                    {
                        "read_only": read_only_command,
                        "files": file_hints,
                        "new_files": new_file_hints_count,
                    }
                )
                if len(recent_iteration_signals) > settings.stall_window_iterations:
                    recent_iteration_signals = recent_iteration_signals[
                        -settings.stall_window_iterations :
                    ]
                if shell_result.exit_code == 0:
                    budget_ledger.failed_command_attempts.pop(command_budget_key, None)
                else:
                    budget_ledger.failed_command_attempts[command_budget_key] = (
                        previous_failures + 1
                    )
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
                            redactor=redactor,
                        ),
                    )
                )

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
            has_stall_signals = all_recent_read_only and (
                no_new_files_recently or repeated_same_file_reads
            )
            if has_stall_signals:
                if stall_correction_injected_at is None and settings.stall_correction_turns > 0:
                    messages.append(
                        CliRuntimeMessage(
                            role="assistant",
                            content=(
                                "Runtime corrective message: progress appears stalled. "
                                "Please stop rereading and do one now: "
                                "(1) concise plan, (2) first concrete edit, or "
                                "(3) final answer with findings and missing info."
                            ),
                        )
                    )
                    stall_correction_injected_at = iteration
                    set_span_status(STATUS_OK)
                    continue
                if (
                    stall_correction_injected_at is not None
                    and iteration - stall_correction_injected_at <= settings.stall_correction_turns
                ):
                    set_span_status(STATUS_OK)
                    continue
                _update_budget_ledger(
                    budget_ledger,
                    started_at=started_at,
                    clock=clock,
                    iterations_used=iteration,
                )
                if commands_with_writes == 0:
                    return CliRuntimeExecutionResult(
                        status="failure",
                        summary=(
                            "CLI runtime consumed iterations without meaningful task progress "
                            "before budget exhaustion."
                        ),
                        stop_reason="no_progress_before_budget",
                        commands_run=commands_run,
                        messages=messages,
                        budget_ledger=budget_ledger,
                    )
                return CliRuntimeExecutionResult(
                    status="failure",
                    summary=(
                        "CLI runtime stalled in repeated inspection without converging to "
                        "concrete edits or a final answer."
                    ),
                    stop_reason="stalled_in_inspection",
                    commands_run=commands_run,
                    messages=messages,
                    budget_ledger=budget_ledger,
                )
            set_span_status(STATUS_OK)

    exhausted_without_progress = commands_run and commands_with_writes == 0
    return CliRuntimeExecutionResult(
        status="failure",
        summary=(
            "CLI runtime consumed iterations without meaningful task progress "
            "before budget exhaustion."
            if exhausted_without_progress
            else (
                "CLI runtime hit its max iteration budget "
                f"({settings.max_iterations}) before reaching a final answer."
            )
        ),
        stop_reason="no_progress_before_budget" if exhausted_without_progress else "max_iterations",
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
