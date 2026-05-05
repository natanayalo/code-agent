"""Independent verifier helpers for orchestrator verification stages."""

from __future__ import annotations

import logging
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from tools.numeric import coerce_positive_int_like

if TYPE_CHECKING:
    from orchestrator.state import OrchestratorState

logger = logging.getLogger(__name__)

DEFAULT_INDEPENDENT_VERIFIER_TIMEOUT_SECONDS = 120
DEFAULT_INDEPENDENT_VERIFIER_MAX_COMMANDS = 3
DEFAULT_INDEPENDENT_VERIFIER_OUTPUT_PREVIEW_BYTES = 4096

_DISALLOWED_SHELL_MARKERS = (
    "&&",
    "||",
    ";",
    "|",
    ">",
    "<",
    "$(",
    "`",
)
_READ_ONLY_VERIFIER_COMMAND_PREFIXES = (
    "ls",
    "cat ",
    "head ",
    "tail ",
    "wc ",
    "find ",
    "grep ",
    "rg ",
    "sed -n",
    "git status",
    "git diff",
    "git show",
    "pytest",
    ".venv/bin/pytest",
    "python -m pytest",
    ".venv/bin/python -m pytest",
    "poetry run pytest",
    "npm test",
    "npm run test",
    "npm run test:run",
    "npm run test:coverage",
    "pnpm test",
    "pnpm run test",
    "pnpm run test:run",
    "pnpm run test:coverage",
    "vitest",
    "npx vitest",
    "ruff check",
    ".venv/bin/ruff check",
    "mypy",
    ".venv/bin/mypy",
)


def _workspace_path_from_result_artifacts(state: OrchestratorState) -> Path | None:
    """Resolve workspace artifact URI to a local path for verifier execution."""
    if not state.result or not state.result.artifacts:
        return None

    for artifact in state.result.artifacts:
        if artifact.name != "workspace" or not artifact.uri.startswith("file://"):
            continue
        parsed = urlparse(artifact.uri)
        decoded_path = unquote(parsed.path)
        path_text = url2pathname(decoded_path)
        # Handle file:///C:/... style URIs robustly across host OSes.
        if (
            len(path_text) >= 3
            and path_text[0] == "/"
            and path_text[1].isalpha()
            and path_text[2] == ":"
        ):
            path_text = path_text[1:]
        return Path(path_text)
    return None


def _normalize_verification_commands(raw: object) -> list[str]:
    """Normalize verification command inputs into stripped command strings."""
    if isinstance(raw, str):
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if not isinstance(raw, list | tuple):
        return []
    commands: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        command = item.strip()
        if command:
            commands.append(command)
    return commands


def resolve_verification_commands(state: OrchestratorState) -> list[str]:
    """Resolve verifier commands from task spec first, then constraints fallback."""
    if state.task_spec is not None:
        commands = _normalize_verification_commands(state.task_spec.verification_commands)
        if commands:
            return commands
    return _normalize_verification_commands(state.task.constraints.get("verification_commands"))


def _looks_read_only_verifier_command(command: str) -> bool:
    """Constrain independent verifier commands to read-only/default-safe shapes."""
    normalized = " ".join(command.strip().split()).lower()
    if not normalized:
        return False
    if any(marker in normalized for marker in _DISALLOWED_SHELL_MARKERS):
        return False
    return any(
        normalized == prefix.rstrip() or normalized.startswith(prefix.rstrip() + " ")
        for prefix in _READ_ONLY_VERIFIER_COMMAND_PREFIXES
    )


def _read_output_preview(file_obj: IO[bytes]) -> str:
    """Read a bounded output preview from a temporary command-output stream."""
    file_obj.seek(0)
    content = file_obj.read(DEFAULT_INDEPENDENT_VERIFIER_OUTPUT_PREVIEW_BYTES)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace").strip()
    return str(content).strip()


def _run_command(command: str, *, workspace_path: Path, timeout_seconds: int) -> tuple[int, str]:
    """Execute one verifier command in the workspace and return (exit code, details)."""
    command_argv = shlex.split(command)
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        completed = subprocess.run(
            command_argv,
            cwd=workspace_path,
            stdout=stdout_file,
            stderr=stderr_file,
            timeout=timeout_seconds,
            check=False,
        )
        stderr_preview = _read_output_preview(stderr_file)
        stdout_preview = _read_output_preview(stdout_file)
    detail = stderr_preview or stdout_preview
    return completed.returncode, detail


def run_independent_verifier(
    state: OrchestratorState,
) -> tuple[Literal["passed", "failed", "warning"], str]:
    """Execute safe verifier commands and return `(status, summary_message)`."""
    commands = resolve_verification_commands(state)
    if not commands:
        return "warning", "Independent verifier enabled, but no verification commands were set."

    workspace_path = _workspace_path_from_result_artifacts(state)
    if workspace_path is None or not workspace_path.exists():
        return "warning", "Independent verifier skipped: workspace artifact path unavailable."

    budget = state.task.budget if isinstance(state.task.budget, dict) else {}
    timeout_seconds = (
        coerce_positive_int_like(budget.get("independent_verifier_timeout_seconds"))
        or DEFAULT_INDEPENDENT_VERIFIER_TIMEOUT_SECONDS
    )
    max_commands = (
        coerce_positive_int_like(budget.get("independent_verifier_max_commands"))
        or DEFAULT_INDEPENDENT_VERIFIER_MAX_COMMANDS
    )
    selected_commands = commands[:max_commands]

    passed = 0
    failed = 0
    skipped = 0
    failure_details: list[str] = []
    for command in selected_commands:
        if not _looks_read_only_verifier_command(command):
            skipped += 1
            logger.info("Independent verifier skipped unsafe command", extra={"command": command})
            continue

        try:
            exit_code, detail = _run_command(
                command,
                workspace_path=workspace_path,
                timeout_seconds=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            failed += 1
            failure_details.append(f"`{command}` timed out after {timeout_seconds}s")
            continue
        except FileNotFoundError as exc:
            failed += 1
            failure_details.append(f"`{command}` failed to start ({exc})")
            continue
        except ValueError as exc:
            failed += 1
            failure_details.append(f"`{command}` could not be parsed ({exc})")
            continue
        except Exception as exc:  # pragma: no cover - defensive boundary logging
            failed += 1
            failure_details.append(f"`{command}` failed unexpectedly ({type(exc).__name__})")
            logger.warning(
                "Independent verifier command failed unexpectedly",
                exc_info=True,
                extra={"command": command},
            )
            continue

        if exit_code == 0:
            passed += 1
        else:
            failed += 1
            detail_preview = f": {detail[:200]}" if detail else ""
            failure_details.append(f"`{command}` exited with {exit_code}{detail_preview}")

    total = len(selected_commands)
    if failed > 0:
        first_failure = failure_details[0] if failure_details else "no failure detail captured"
        return (
            "failed",
            (
                "Independent verifier failed "
                f"({failed} failed, {passed} passed, {skipped} skipped, {total} selected). "
                f"First failure: {first_failure}"
            ),
        )

    if skipped > 0:
        return (
            "warning",
            (
                "Independent verifier completed with skipped command(s) "
                f"({passed} passed, {skipped} skipped, {total} selected)."
            ),
        )

    return (
        "passed",
        f"Independent verifier passed ({passed} command(s) succeeded).",
    )
