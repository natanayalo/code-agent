"""Changed-file collection helpers for the shared CLI runtime."""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import Path

from sandbox import DockerShellSessionError
from workers.adapter_utils import truncate_detail_keep_tail
from workers.cli_runtime_types import ShellSessionProtocol
from workers.constants import DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)
_GIT_ERROR_OUTPUT_MAX_CHARACTERS = 2048
_UNTRACKED_NATIVE_RUNTIME_PREFIXES = (
    ".agent_home/",
    ".code-agent/native-agent-runner/",
)


def _is_untracked_native_runtime_path(status: str, path: str) -> bool:
    """Return whether an untracked path is created by a native worker runtime."""
    if status != "??":
        return False
    return path in {".agent_home", ".code-agent/native-agent-runner"} or path.startswith(
        _UNTRACKED_NATIVE_RUNTIME_PREFIXES
    )


def _decode_safely(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


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


def _execute_porcelain_z(
    session: ShellSessionProtocol,
    command: str,
    timeout_seconds: int,
) -> list[str] | None:
    try:
        status_result = session.execute(command, timeout_seconds=timeout_seconds)
    except DockerShellSessionError:
        logger.warning(
            "CLI runtime failed to collect changed files from git status with porcelain -z; "
            "falling back to line-delimited output."
        )
        return None

    if status_result.exit_code == 0:
        return _parse_porcelain_z(status_result.output)

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
    return None


def _execute_porcelain_lines(
    session: ShellSessionProtocol,
    command: str,
    timeout_seconds: int,
) -> list[str] | None:
    try:
        fallback_result = session.execute(command, timeout_seconds=timeout_seconds)
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

    return _parse_porcelain_lines(fallback_result.output)


def collect_changed_files(
    session: ShellSessionProtocol,
    *,
    working_directory: Path | None = None,
    timeout_seconds: int = DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS,
) -> list[str]:
    """Collect changed paths from the git workspace when available."""
    git_command_prefix = (
        f"git -C {shlex.quote(str(working_directory))}" if working_directory is not None else "git"
    )
    porcelain_z_command = f"{git_command_prefix} status --porcelain=v1 -z --untracked-files=all"
    fallback_command = f"{git_command_prefix} status --porcelain=v1 --untracked-files=all"

    z_result = _execute_porcelain_z(session, porcelain_z_command, timeout_seconds)
    if z_result is not None:
        return list(dict.fromkeys(z_result))

    lines_result = _execute_porcelain_lines(session, fallback_command, timeout_seconds)
    if lines_result is not None:
        return list(dict.fromkeys(lines_result))

    return []


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
        raw_output = _decode_safely(completed.stdout) + _decode_safely(completed.stderr)
        output_preview = truncate_detail_keep_tail(
            raw_output,
            max_characters=_GIT_ERROR_OUTPUT_MAX_CHARACTERS,
        )
        if _git_status_unavailable(raw_output):
            logger.info(
                "Worker skipped host-side changed-file collection because workspace is not a "
                "usable git repository.",
                extra={"exit_code": completed.returncode},
            )
            return []
        logger.warning(
            "Worker could not collect changed files via host git status.",
            extra={"exit_code": completed.returncode, "output": output_preview},
        )
        return []

    changed_files: list[str] = []
    items = iter(_decode_safely(completed.stdout).split("\0"))
    for item in items:
        if len(item) < 4:
            continue
        status = item[:2]
        path = item[3:]
        if "R" in status or "C" in status:
            next(items, None)
        if path and not _is_untracked_native_runtime_path(status, path):
            changed_files.append(path)

    return list(dict.fromkeys(changed_files))


def collect_changed_files_since_ref_from_repo_path(
    repo_path: Path,
    *,
    base_ref: str | None,
    timeout_seconds: int = DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS,
) -> list[str]:
    """Collect paths changed between a starting git ref and the current workspace."""
    working_tree_files = collect_changed_files_from_repo_path(
        repo_path,
        timeout_seconds=timeout_seconds,
    )
    if not base_ref:
        return working_tree_files

    command = [
        "git",
        "-C",
        str(repo_path),
        "diff",
        "--name-only",
        "-z",
        base_ref,
        "HEAD",
        "--",
        ".",
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
            "Worker git diff timed out while collecting changed files from baseline.",
            extra={"timeout_seconds": timeout_seconds},
            exc_info=exc,
        )
        return working_tree_files
    except OSError as exc:
        logger.warning(
            "Worker failed to collect baseline changed files via host git diff.",
            exc_info=exc,
        )
        return working_tree_files

    if completed.returncode != 0:
        raw_output = _decode_safely(completed.stdout) + _decode_safely(completed.stderr)
        output_preview = truncate_detail_keep_tail(
            raw_output,
            max_characters=_GIT_ERROR_OUTPUT_MAX_CHARACTERS,
        )
        if _git_status_unavailable(raw_output):
            logger.info(
                "Worker skipped baseline changed-file collection because workspace is not a "
                "usable git repository.",
                extra={"exit_code": completed.returncode},
            )
            return working_tree_files
        logger.warning(
            "Worker could not collect changed files from baseline via host git diff.",
            extra={"exit_code": completed.returncode, "output": output_preview},
        )
        return working_tree_files

    baseline_files = [path for path in _decode_safely(completed.stdout).split("\0") if path]
    return list(dict.fromkeys([*baseline_files, *working_tree_files]))
