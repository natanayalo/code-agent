"""Context-window and command-inspection helpers for the shared CLI runtime."""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from workers.cli_runtime_types import CliRuntimeMessage, CliRuntimeSettings
from workers.constants import (
    DEFAULT_CONDENSED_SUMMARY_MAX_DECISIONS,
    DEFAULT_CONDENSED_SUMMARY_MAX_ERRORS,
    DEFAULT_CONDENSED_SUMMARY_MAX_FILE_HINTS,
    DEFAULT_CONTEXT_WINDOW_WARNING_RATIO,
    DEFAULT_ESTIMATED_CHARACTERS_PER_TOKEN,
)

logger = logging.getLogger(__name__)

MODEL_CONTEXT_WINDOW_TOKENS: dict[str, int] = {
    "gpt-5.4": 272000,
    "gemini-2.5-pro": 1048576,
}
READ_ONLY_COMMAND_PREFIXES = (
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
WRITE_COMMAND_MARKERS = (
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
FILE_ARGUMENT_COMMANDS = frozenset(
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
COMMANDS_WITH_LEADING_NON_PATH_ARGUMENT = frozenset({"awk", "chmod", "chown", "grep", "sed"})
GIT_FILE_ARGUMENT_SUBCOMMANDS = frozenset({"add", "mv", "restore", "rm"})


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
            primary_command in COMMANDS_WITH_LEADING_NON_PATH_ARGUMENT
            and command_argument_index == 0
        ):
            command_argument_index += 1
            continue
        if "/" in candidate or "." in Path(candidate).name:
            hints.append(candidate)
            command_argument_index += 1
            continue
        if (
            primary_command in FILE_ARGUMENT_COMMANDS
            and "=" not in candidate
            and candidate != primary_command
        ):
            if primary_command == "git" and git_subcommand not in GIT_FILE_ARGUMENT_SUBCOMMANDS:
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
    if any(marker in normalized for marker in WRITE_COMMAND_MARKERS):
        return False
    return any(
        normalized == prefix.rstrip() or normalized.startswith(prefix.rstrip() + " ")
        for prefix in READ_ONLY_COMMAND_PREFIXES
    )


def _inline_code(value: str) -> str:
    """Render inline code while safely handling content that contains backticks."""
    max_tick_run = max((len(match.group(0)) for match in re.finditer(r"`+", value)), default=0)
    fence = "`" * (max_tick_run + 1)
    space = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{space}{value}{space}{fence}"


def _extract_condensed_history_elements(
    older_messages: Sequence[CliRuntimeMessage],
) -> tuple[list[str], list[str], list[str]]:
    """Extract and deduplicate decisions, touched files, and errors from history."""
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
    return deduped_decisions, deduped_files, deduped_errors


def _extract_current_working_state(older_messages: Sequence[CliRuntimeMessage]) -> str:
    """Find the most recent command execution status from history."""
    for message in reversed(older_messages):
        if message.role != "tool":
            continue
        observed_command = _extract_prefixed_line(message.content, prefix="Command: ")
        raw_exit_code = _extract_prefixed_line(message.content, prefix="Exit code: ")
        if observed_command and raw_exit_code:
            return f"last command {_inline_code(observed_command)} exited with code {raw_exit_code}"
    return "no tool state available from condensed history"


def _format_condensed_summary_text(
    deduped_decisions: list[str],
    deduped_files: list[str],
    deduped_errors: list[str],
    current_state: str,
) -> str:
    """Format the condensed context summary components into a readable block."""
    lines = ["Condensed context summary (older iterations):"]

    if deduped_decisions:
        decisions_list = ", ".join(
            _inline_code(command)
            for command in deduped_decisions[-DEFAULT_CONDENSED_SUMMARY_MAX_DECISIONS:]
        )
        lines.append(f"- Key decisions made: {decisions_list}")
    else:
        lines.append("- Key decisions made: none")

    if deduped_files:
        files_list = ", ".join(
            _inline_code(path) for path in deduped_files[-DEFAULT_CONDENSED_SUMMARY_MAX_FILE_HINTS:]
        )
        lines.append(f"- Files touched hints: {files_list}")
    else:
        lines.append("- Files touched hints: none")

    if deduped_errors:
        errors_list = ", ".join(deduped_errors[-DEFAULT_CONDENSED_SUMMARY_MAX_ERRORS:])
        lines.append(f"- Errors encountered: {errors_list}")
    else:
        lines.append("- Errors encountered: none")

    lines.append(f"- Current working state: {current_state}")
    lines.append("Recent raw messages follow unchanged.")
    return "\n".join(lines)


def _apply_summary_truncation(summary: str, max_characters: int) -> str:
    """Safely bound the summary to max_characters while preserving truncation warning."""
    if len(summary) <= max_characters:
        return summary

    suffix = f"\n[condensed summary truncated to {max_characters} characters]"
    available_for_summary = max_characters - len(suffix)
    if available_for_summary <= 0:
        suffix_only, _ = _truncate_text(suffix, max_characters=max_characters)
        return suffix_only
    bounded_summary, _ = _truncate_text(summary, max_characters=available_for_summary)
    return f"{bounded_summary}{suffix}"


def _build_condensed_context_summary(
    older_messages: Sequence[CliRuntimeMessage],
    *,
    max_characters: int,
) -> str:
    """Build deterministic condensed context for older loop iterations."""
    deduped_decisions, deduped_files, deduped_errors = _extract_condensed_history_elements(
        older_messages
    )
    current_state = _extract_current_working_state(older_messages)

    summary = _format_condensed_summary_text(
        deduped_decisions=deduped_decisions,
        deduped_files=deduped_files,
        deduped_errors=deduped_errors,
        current_state=current_state,
    )
    return _apply_summary_truncation(summary, max_characters)


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
