from __future__ import annotations

_SAFE_READ_ONLY_COMMANDS = frozenset({"cat", "head", "ls", "pwd", "tail", "wc"})

_SAFE_GREP_EXECUTABLES = frozenset({"egrep", "fgrep", "grep"})

_SAFE_RG_BLOCKLIST = frozenset({"--pre", "--pre-glob"})

_SAFE_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {"status", "log", "diff", "show", "ls-files", "rev-parse", "blame"}
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
