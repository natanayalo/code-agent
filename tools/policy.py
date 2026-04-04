"""Permission policy helpers for tool execution."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from tools.registry import ToolDefinition, ToolModel, ToolPermissionLevel

_PERMISSION_ORDER = {
    ToolPermissionLevel.READ_ONLY: 0,
    ToolPermissionLevel.WORKSPACE_WRITE: 1,
    ToolPermissionLevel.DANGEROUS_SHELL: 2,
    ToolPermissionLevel.NETWORKED_WRITE: 3,
    ToolPermissionLevel.GIT_PUSH_OR_DEPLOY: 4,
}

_TOKEN_PREFIX_PERMISSION_RULES: tuple[
    tuple[ToolPermissionLevel, str, tuple[tuple[str, ...], ...]],
    ...,
] = (
    (
        ToolPermissionLevel.GIT_PUSH_OR_DEPLOY,
        "Command pushes changes or performs a deploy-like action.",
        (
            ("git", "push"),
            ("kubectl", "apply"),
            ("terraform", "apply"),
            ("fly", "deploy"),
            ("flyctl", "deploy"),
            ("deploy",),
            ("release",),
        ),
    ),
    (
        ToolPermissionLevel.NETWORKED_WRITE,
        "Command requires network access.",
        (
            ("curl",),
            ("wget",),
            ("ssh",),
            ("scp",),
            ("rsync",),
            ("nc",),
            ("pip", "install"),
            ("uv", "pip", "install"),
            ("npm", "install"),
            ("npm", "add"),
            ("pnpm", "install"),
            ("pnpm", "add"),
            ("yarn", "install"),
            ("yarn", "add"),
            ("brew", "install"),
            ("git", "clone"),
            ("git", "pull"),
            ("git", "fetch"),
            ("git", "ls-remote"),
        ),
    ),
    (
        ToolPermissionLevel.DANGEROUS_SHELL,
        "Command executes shell or interpreter code that cannot be safely classified.",
        (
            ("alias",),
            ("function",),
            ("bash",),
            ("sh",),
            ("zsh",),
            ("python", "-c"),
            ("python3", "-c"),
            ("node", "-e"),
            ("node", "--eval"),
            ("perl", "-e"),
            ("ruby", "-e"),
            ("php", "-r"),
        ),
    ),
    (
        ToolPermissionLevel.DANGEROUS_SHELL,
        "Command performs a destructive shell or git operation.",
        (
            ("rm",),
            ("rmdir",),
            ("eval",),
            ("source",),
            (".",),
            ("git", "clean"),
            ("git", "reset"),
            ("mkfs",),
            ("dd",),
        ),
    ),
)

_SHELL_OPERATOR_TOKENS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"})
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_ENV_WRAPPER_OPTIONS_WITH_ARGUMENT = frozenset(
    {
        "-c",
        "-s",
        "-u",
        "--chdir",
        "--default-signal",
        "--ignore-signal",
        "--split-string",
        "--unset",
    }
)
_NICE_WRAPPER_OPTIONS_WITH_ARGUMENT = frozenset({"-n"})
_SIMPLE_WRAPPER_COMMANDS = frozenset({"builtin", "command", "nohup", "time"})
_SUDO_WRAPPER_OPTIONS_WITH_ARGUMENT = frozenset(
    {
        "-c",
        "-d",
        "-g",
        "-h",
        "-p",
        "-r",
        "-t",
        "-u",
        "--chdir",
        "--group",
        "--host",
        "--other-user",
        "--prompt",
        "--role",
        "--type",
        "--user",
    }
)
_SAFE_READ_ONLY_COMMANDS = frozenset({"cat", "head", "ls", "pwd", "tail", "wc"})
_SAFE_GREP_EXECUTABLES = frozenset({"egrep", "fgrep", "grep"})
_SAFE_RG_BLOCKLIST = frozenset({"--pre", "--pre-glob"})
_UNSUPPORTED_COMMAND_WORD_GLOB_CHARS = frozenset({"*", "?", "[", "{"})
_SAFE_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {"status", "log", "diff", "show", "ls-files", "rev-parse", "blame"}
)
_WRITE_REDIRECTION_OPERATORS = frozenset({">", ">>"})


class ToolPermissionDecision(ToolModel):
    """Resolved permission requirement for a concrete tool invocation."""

    tool_name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    granted_permission: ToolPermissionLevel
    required_permission: ToolPermissionLevel
    allowed: bool
    reason: str = Field(min_length=1)


def permission_rank(level: ToolPermissionLevel) -> int:
    """Return a stable rank for permission comparisons."""
    return _PERMISSION_ORDER[level]


def permission_allows(
    granted_permission: ToolPermissionLevel,
    required_permission: ToolPermissionLevel,
) -> bool:
    """Return whether a granted permission level satisfies the requirement."""
    return permission_rank(granted_permission) >= permission_rank(required_permission)


def _coerce_permission_level(value: object) -> ToolPermissionLevel | None:
    """Parse a permission level from strings or enum values."""
    if isinstance(value, ToolPermissionLevel):
        return value
    if isinstance(value, str):
        normalized_value = value.strip().lower()
        if not normalized_value:
            return None
        try:
            return ToolPermissionLevel(normalized_value)
        except ValueError:
            return None
    return None


def _default_permission_reason(default_permission: ToolPermissionLevel) -> str:
    """Render the fallback reason when a command stays at the tool's default level."""
    return "Command uses the tool's default permission level " f"({default_permission.value})."


def _unsupported_shell_feature_reason() -> str:
    """Explain why unsupported shell syntax fails closed."""
    return "Command uses unsupported shell features that cannot be safely classified."


def _unparseable_shell_command_reason() -> str:
    """Explain why malformed shell syntax fails closed."""
    return (
        "Command could not be safely parsed and is treated as requiring elevated "
        "shell permission."
    )


def granted_permission_from_constraints(
    constraints: Mapping[str, Any],
    *,
    default: ToolPermissionLevel = ToolPermissionLevel.WORKSPACE_WRITE,
) -> ToolPermissionLevel:
    """Resolve the currently granted permission level from worker constraints."""
    for key in ("granted_permission", "allowed_permission_level", "permission_level"):
        resolved = _coerce_permission_level(constraints.get(key))
        if resolved is not None:
            return resolved
    return default


def _command_lexemes(command: str) -> tuple[str, ...] | None:
    """Parse shell lexemes while preserving control operators as standalone tokens."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        return tuple(lexer)
    except ValueError:
        return None


def _matches_token_prefix(tokens: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    """Return whether a tokenized command starts with the given normalized prefix."""
    return len(tokens) >= len(prefix) and tokens[: len(prefix)] == prefix


def _classify_token_prefix_permission(
    tokens: tuple[str, ...],
) -> tuple[ToolPermissionLevel, str] | None:
    """Resolve command permissions from normalized executable/subcommand prefixes."""
    for permission_level, reason, prefixes in _TOKEN_PREFIX_PERMISSION_RULES:
        if any(_matches_token_prefix(tokens, prefix) for prefix in prefixes):
            return permission_level, reason
    return None


def _command_uses_unsupported_shell_features(command: str) -> bool:
    """Return whether the command relies on shell features we do not safely classify."""
    return (
        "$(" in command
        or "`" in command
        or "<(" in command
        or ">(" in command
        or _command_uses_grouping_parens(command)
        or _command_word_uses_unsupported_expansion(command)
        or "\n" in command
        or "\r" in command
    )


def _command_uses_grouping_parens(command: str) -> bool:
    """Return whether the command uses unquoted grouping parens."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        lexemes = tuple(lexer)
    except ValueError:
        return False
    return "(" in lexemes or ")" in lexemes


def _command_word_uses_unsupported_expansion(command: str) -> bool:
    """Return whether any segment's command word uses expansion we do not safely classify."""
    lexemes = _command_lexemes(command)
    if lexemes is None:
        return False

    expect_command_word = True
    for lexeme in lexemes:
        if lexeme in _SHELL_OPERATOR_TOKENS:
            expect_command_word = True
            continue
        if not expect_command_word:
            continue
        expect_command_word = False
        if "$" in lexeme or any(char in lexeme for char in _UNSUPPORTED_COMMAND_WORD_GLOB_CHARS):
            return True
    return False


def _command_segments(command: str) -> tuple[tuple[str, ...], ...] | None:
    """Split a shell command into operator-delimited segments."""
    lexemes = _command_lexemes(command)
    if lexemes is None:
        return None

    segments: list[tuple[str, ...]] = []
    current: list[str] = []
    for lexeme in lexemes:
        if lexeme in _SHELL_OPERATOR_TOKENS:
            if current:
                segments.append(tuple(current))
                current = []
            continue
        current.append(lexeme)

    if current:
        segments.append(tuple(current))
    return tuple(segments)


def _normalize_command_word(token: str) -> str:
    """Normalize a command word for rule matching."""
    return posixpath.basename(token).lower()


def _strip_leading_assignments(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Drop leading env-style assignments before the executable."""
    index = 0
    while index < len(tokens) and _ENV_ASSIGNMENT_PATTERN.match(tokens[index]):
        index += 1
    return tokens[index:]


def _strip_wrapper_options(
    tokens: tuple[str, ...],
    *,
    options_with_argument: frozenset[str],
) -> tuple[str, ...]:
    """Drop wrapper-specific options until the wrapped command begins."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered_token = token.lower()
        if token == "--":
            return tokens[index + 1 :]
        if not token.startswith("-") or token == "-":
            return tokens[index:]
        if lowered_token in options_with_argument:
            index += 2
            continue
        if any(lowered_token.startswith(f"{option}=") for option in options_with_argument):
            index += 1
            continue
        index += 1
    return ()


def _max_permission_level(
    first: ToolPermissionLevel,
    second: ToolPermissionLevel,
) -> ToolPermissionLevel:
    """Return the higher-ranked permission level."""
    if permission_rank(first) >= permission_rank(second):
        return first
    return second


def _unwrap_command_tokens(
    tokens: tuple[str, ...],
) -> tuple[tuple[str, ...], ToolPermissionLevel | None]:
    """Strip known wrappers while preserving any permission floor they imply."""
    remaining = _strip_leading_assignments(tokens)
    wrapper_floor: ToolPermissionLevel | None = None

    while remaining:
        executable = _normalize_command_word(remaining[0])
        wrapped_tokens = remaining[1:]
        if executable == "sudo":
            wrapper_floor = (
                ToolPermissionLevel.DANGEROUS_SHELL
                if wrapper_floor is None
                else _max_permission_level(
                    wrapper_floor,
                    ToolPermissionLevel.DANGEROUS_SHELL,
                )
            )
            remaining = _strip_leading_assignments(
                _strip_wrapper_options(
                    wrapped_tokens,
                    options_with_argument=_SUDO_WRAPPER_OPTIONS_WITH_ARGUMENT,
                )
            )
            continue
        if executable == "env":
            remaining = _strip_leading_assignments(
                _strip_wrapper_options(
                    wrapped_tokens,
                    options_with_argument=_ENV_WRAPPER_OPTIONS_WITH_ARGUMENT,
                )
            )
            continue
        if executable == "nice":
            remaining = _strip_leading_assignments(
                _strip_wrapper_options(
                    wrapped_tokens,
                    options_with_argument=_NICE_WRAPPER_OPTIONS_WITH_ARGUMENT,
                )
            )
            continue
        if executable in _SIMPLE_WRAPPER_COMMANDS:
            remaining = _strip_leading_assignments(
                _strip_wrapper_options(
                    wrapped_tokens,
                    options_with_argument=frozenset(),
                )
            )
            continue
        break

    return remaining, wrapper_floor


def _normalized_tokens_for_matching(
    tokens: tuple[str, ...],
) -> tuple[tuple[str, ...], ToolPermissionLevel | None]:
    """Normalize a segment into matchable tokens and any wrapper-imposed floor."""
    remaining, wrapper_floor = _unwrap_command_tokens(tokens)
    if not remaining:
        return (), wrapper_floor
    return (
        (_normalize_command_word(remaining[0]), *(token.lower() for token in remaining[1:])),
        wrapper_floor,
    )


def _is_safe_rg_command(tokens: tuple[str, ...]) -> bool:
    """Allow plain ripgrep searches while rejecting flags that spawn helpers."""
    return not any(
        token in _SAFE_RG_BLOCKLIST
        or any(token.startswith(f"{flag}=") for flag in _SAFE_RG_BLOCKLIST)
        for token in tokens[1:]
    )


def _is_safe_git_read_only_command(tokens: tuple[str, ...]) -> bool:
    """Allow a narrow set of read-only git subcommands."""
    if len(tokens) < 2:
        return False

    subcommand = tokens[1]
    if subcommand in _SAFE_GIT_READ_ONLY_SUBCOMMANDS:
        return True
    if subcommand != "grep":
        return False
    return len(tokens) > 2 and all(token != "-" for token in tokens[2:])


def _is_safe_grep_command(tokens: tuple[str, ...]) -> bool:
    """Allow simple grep-style searches that include an explicit search path."""
    return (
        len(tokens) >= 3
        and tokens[0] in _SAFE_GREP_EXECUTABLES
        and not tokens[1].startswith("-")
        and all(token != "-" for token in tokens[2:])
    )


def _command_uses_write_redirection(command: str) -> bool:
    """Return whether the command writes through shell redirection operators."""
    lexemes = _command_lexemes(command)
    if lexemes is None:
        return False
    return any(lexeme in _WRITE_REDIRECTION_OPERATORS for lexeme in lexemes)


def _is_safe_read_only_command(normalized_tokens: tuple[str, ...]) -> bool:
    """Return whether a command matches the narrow read-only allowlist."""
    executable = normalized_tokens[0]
    if executable in _SAFE_READ_ONLY_COMMANDS:
        return executable in {"ls", "pwd"} or len(normalized_tokens) > 1
    if executable in _SAFE_GREP_EXECUTABLES:
        return _is_safe_grep_command(normalized_tokens)
    if executable == "git":
        return _is_safe_git_read_only_command(normalized_tokens)
    if executable == "rg":
        return _is_safe_rg_command(normalized_tokens)
    return False


def _classify_segment_permission(
    segment_tokens: tuple[str, ...],
    *,
    default_permission: ToolPermissionLevel,
) -> tuple[ToolPermissionLevel, str]:
    """Return the required permission level for one shell segment."""
    normalized_tokens, wrapper_floor = _normalized_tokens_for_matching(segment_tokens)
    if not normalized_tokens:
        return (
            wrapper_floor or default_permission,
            _default_permission_reason(default_permission),
        )

    token_prefix_decision = _classify_token_prefix_permission(normalized_tokens)
    if token_prefix_decision is not None:
        if wrapper_floor is None:
            return token_prefix_decision
        permission_level, reason = token_prefix_decision
        return _max_permission_level(permission_level, wrapper_floor), reason
    if _is_safe_read_only_command(normalized_tokens):
        if wrapper_floor is None:
            return ToolPermissionLevel.READ_ONLY, "Command matches the narrow read-only allowlist."
        return (
            _max_permission_level(ToolPermissionLevel.READ_ONLY, wrapper_floor),
            "Command matches the narrow read-only allowlist.",
        )
    if wrapper_floor is not None:
        return wrapper_floor, "Command uses a wrapper that requires elevated shell permission."
    return (
        default_permission,
        _default_permission_reason(default_permission),
    )


def _classify_bash_command_permission(
    command: str,
    *,
    default_permission: ToolPermissionLevel,
) -> tuple[ToolPermissionLevel, str]:
    """Return the required permission level for a bash command."""
    if _command_uses_unsupported_shell_features(command):
        return (
            _max_permission_level(default_permission, ToolPermissionLevel.DANGEROUS_SHELL),
            _unsupported_shell_feature_reason(),
        )

    segments = _command_segments(command)
    if segments is None:
        return (
            _max_permission_level(default_permission, ToolPermissionLevel.DANGEROUS_SHELL),
            _unparseable_shell_command_reason(),
        )
    if not segments:
        return (
            default_permission,
            _default_permission_reason(default_permission),
        )

    highest_permission = (
        ToolPermissionLevel.WORKSPACE_WRITE if _command_uses_write_redirection(command) else None
    )
    highest_reason = (
        "Command writes via shell redirection."
        if highest_permission is not None
        else _default_permission_reason(default_permission)
    )
    for segment_tokens in segments:
        segment_permission, segment_reason = _classify_segment_permission(
            segment_tokens,
            default_permission=default_permission,
        )
        if highest_permission is None or (
            permission_rank(segment_permission) > permission_rank(highest_permission)
        ):
            highest_permission = segment_permission
            highest_reason = segment_reason

    return highest_permission or default_permission, highest_reason


def resolve_bash_command_permission(
    command: str,
    tool: ToolDefinition,
    *,
    granted_permission: ToolPermissionLevel,
) -> ToolPermissionDecision:
    """Resolve whether a concrete bash command is allowed under the granted permission."""
    required_permission, reason = _classify_bash_command_permission(
        command,
        default_permission=tool.required_permission,
    )
    return ToolPermissionDecision(
        tool_name=tool.name,
        command=command,
        granted_permission=granted_permission,
        required_permission=required_permission,
        allowed=permission_allows(granted_permission, required_permission),
        reason=reason,
    )
