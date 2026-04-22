"""Failure taxonomy helpers shared by workers and orchestration logic."""

from __future__ import annotations

from workers.base import FailureKind, WorkerCommand

_TEST_COMMAND_MARKERS = (
    "cargo test",
    "ctest",
    "go test",
    "nosetests",
    "npm test",
    "pnpm test",
    "pytest",
    "python -m unittest",
    "python3 -m unittest",
    "tox",
    "yarn test",
)
_COMPILE_COMMAND_MARKERS = (
    "cargo build",
    "go build",
    "mvn compile",
    "npm run build",
    "pnpm build",
    "python -m py_compile",
    "python3 -m py_compile",
    "python -m compileall",
    "python3 -m compileall",
    "ruff check",
    "tsc",
    "yarn build",
)
_TEST_SUMMARY_MARKERS = (
    "assertionerror",
    "failed tests",
    "test failed",
    "tests failed",
    "traceback",
)
_COMPILE_SUMMARY_MARKERS = (
    "build failed",
    "compile error",
    "compilation failed",
    "nameerror",
    "syntaxerror",
    "typeerror",
    "type error",
)
_AUTH_SUMMARY_MARKERS = (
    "api key",
    "authentication failed",
    "invalid credentials",
    "token expired",
    "unauthorized",
)
_CONTEXT_WINDOW_SUMMARY_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "prompt too long",
    "token limit",
)


def classify_failure_kind(
    *,
    status: str,
    stop_reason: str | None = None,
    summary: str | None = None,
    commands_run: list[WorkerCommand] | None = None,
) -> FailureKind | None:
    """Return a typed failure kind for a worker/runtime outcome."""
    if status == "success":
        return None

    normalized_summary = (summary or "").lower()
    failed_commands = [
        command.command.lower() for command in (commands_run or []) if command.exit_code
    ]

    if stop_reason == "permission_required":
        return "permission_denied"
    if stop_reason == "worker_timeout":
        return "timeout"
    if stop_reason == "budget_exceeded":
        return "budget_exceeded"
    if stop_reason == "context_window":
        return "context_window"
    if stop_reason == "shell_error":
        return "sandbox_infra"

    if _contains_any(normalized_summary, _CONTEXT_WINDOW_SUMMARY_MARKERS):
        return "context_window"
    if _contains_any(normalized_summary, _AUTH_SUMMARY_MARKERS):
        return "provider_auth"
    if _contains_any_in_commands(failed_commands, _TEST_COMMAND_MARKERS) or _contains_any(
        normalized_summary, _TEST_SUMMARY_MARKERS
    ):
        return "test"
    if _contains_any_in_commands(failed_commands, _COMPILE_COMMAND_MARKERS) or _contains_any(
        normalized_summary, _COMPILE_SUMMARY_MARKERS
    ):
        return "compile"
    if stop_reason == "adapter_error":
        return "provider_error"
    if failed_commands:
        return "tool_runtime"
    return "unknown"


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _contains_any_in_commands(commands: list[str], markers: tuple[str, ...]) -> bool:
    for command in commands:
        if any(marker in command for marker in markers):
            return True
    return False
