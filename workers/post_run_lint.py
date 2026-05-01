"""Post-run lint/format helpers for CLI workers."""

from __future__ import annotations

import json
import logging
import re
import shlex
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sandbox import DockerShellSessionError
from sandbox.session import DockerShellCommandResult
from tools.tracing import start_optional_span
from workers.base import ArtifactReference, WorkerCommand
from workers.cli_runtime import (
    CliRuntimeExecutionResult,
    ShellSessionProtocol,
    collect_changed_files,
    collect_changed_files_from_repo_path,
)

_DEFAULT_FALLBACK_TEMPLATE_KEY = "{files}"
_MAKEFILE_CANDIDATES = ("GNUmakefile", "makefile", "Makefile")
logger = logging.getLogger(__name__)


def _collect_changed_files_with_fallback(
    *,
    session: ShellSessionProtocol,
    repo_working_directory: Path,
    repo_path_for_detection: Path,
    timeout_seconds: int,
) -> list[str]:
    """Collect changed files from the session, falling back to host-side git status."""
    changed_files = collect_changed_files(
        session,
        working_directory=repo_working_directory,
        timeout_seconds=timeout_seconds,
    )
    if changed_files:
        return changed_files

    return collect_changed_files_from_repo_path(
        repo_path_for_detection,
        timeout_seconds=timeout_seconds,
    )


def _merge_changed_files(existing_files: Sequence[str], new_files: Sequence[str]) -> list[str]:
    """Merge changed-file lists with stable order and de-duplication."""
    return list(dict.fromkeys([*existing_files, *new_files]))


def merge_post_run_lint_results(
    existing_result: dict[str, Any],
    new_result: dict[str, Any],
) -> dict[str, Any]:
    """Merge post-run lint metadata from multiple passes into one summary."""
    if not existing_result:
        return dict(new_result)
    if not new_result:
        return dict(existing_result)

    existing_commands_raw = existing_result.get("commands")
    new_commands_raw = new_result.get("commands")
    merged_commands: list[Any] = []
    if isinstance(existing_commands_raw, list):
        merged_commands.extend(existing_commands_raw)
    if isinstance(new_commands_raw, list):
        merged_commands.extend(new_commands_raw)

    existing_errors_raw = existing_result.get("errors")
    new_errors_raw = new_result.get("errors")
    merged_errors: list[Any] = []
    if isinstance(existing_errors_raw, list):
        merged_errors.extend(existing_errors_raw)
    if isinstance(new_errors_raw, list):
        merged_errors.extend(new_errors_raw)

    existing_artifacts_raw = existing_result.get("artifacts")
    new_artifacts_raw = new_result.get("artifacts")
    merged_artifacts: list[Any] = []
    if isinstance(existing_artifacts_raw, list):
        merged_artifacts.extend(existing_artifacts_raw)
    if isinstance(new_artifacts_raw, list):
        merged_artifacts.extend(new_artifacts_raw)

    statuses = [status for status in (existing_result.get("status"), new_result.get("status"))]
    if "warning" in statuses:
        merged_status = "warning"
    elif "passed" in statuses:
        merged_status = "passed"
    elif "skipped" in statuses:
        merged_status = "skipped"
    else:
        merged_status = "skipped"

    merged_ran = bool(existing_result.get("ran") or new_result.get("ran"))
    merged_reason = new_result.get("reason")
    if merged_reason in (None, ""):
        merged_reason = existing_result.get("reason")

    return {
        "ran": merged_ran,
        "status": merged_status,
        "reason": None if merged_ran else merged_reason,
        "commands": merged_commands,
        "errors": merged_errors,
        "artifacts": merged_artifacts,
    }


def _python_files_only(files_changed: Sequence[str]) -> list[str]:
    """Filter to Python paths for ruff-based lint/format."""
    return [path for path in files_changed if path.endswith((".py", ".pyi"))]


def _render_file_args(files_changed: Sequence[str]) -> str:
    """Render shell-safe file args for command composition."""
    return " ".join(shlex.quote(path) for path in files_changed)


def _ruff_config_present(repo_path: Path) -> bool:
    """Return True when pyproject.toml declares ruff config."""
    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.is_file():
        return False
    try:
        with pyproject_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    tool_section = payload.get("tool")
    if not isinstance(tool_section, dict):
        return False
    return isinstance(tool_section.get("ruff"), dict)


def _read_package_json_scripts(repo_path: Path) -> dict[str, str]:
    """Return package.json scripts when available."""
    package_json_path = repo_path / "package.json"
    if not package_json_path.is_file():
        return {}
    try:
        payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in scripts.items():
        if isinstance(key, str) and isinstance(value, str):
            normalized[key] = value
    return normalized


def _package_json_lint_commands(
    *,
    repo_path: Path,
    files_changed: Sequence[str],
) -> list[str]:
    """Detect npm script-based lint/format commands scoped to changed files."""
    scripts = _read_package_json_scripts(repo_path)
    if not scripts:
        return []
    file_args = _render_file_args(files_changed)
    if not file_args:
        return []
    commands: list[str] = []
    if "format" in scripts:
        commands.append(f"npm run format -- {file_args}")
    elif "fmt" in scripts:
        commands.append(f"npm run fmt -- {file_args}")
    if "lint:fix" in scripts:
        commands.append(f"npm run lint:fix -- {file_args}")
    elif "lint_fix" in scripts:
        commands.append(f"npm run lint_fix -- {file_args}")
    elif "lint" in scripts:
        commands.append(f"npm run lint -- --fix {file_args}")
    return commands


def _extract_make_targets(content: str) -> set[str]:
    """Extract non-special make targets from a Makefile."""
    targets: set[str] = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("\t") or line.startswith("#"):
            continue
        if line.startswith("."):
            continue
        match = re.match(r"^([A-Za-z0-9_./-]+)\s*:(?:\s|$)", line)
        if match is not None:
            targets.add(match.group(1))
    return targets


def _makefile_targets(repo_path: Path) -> set[str]:
    """Return parsed target names from the first available Makefile variant."""
    for candidate in _MAKEFILE_CANDIDATES:
        makefile_path = repo_path / candidate
        if not makefile_path.is_file():
            continue
        try:
            return _extract_make_targets(makefile_path.read_text(encoding="utf-8"))
        except OSError:
            return set()
    return set()


def _makefile_lint_commands(
    *,
    repo_path: Path,
    files_changed: Sequence[str],
) -> list[str]:
    """Detect Makefile lint/format commands scoped via FILES variable."""
    targets = _makefile_targets(repo_path)
    if not targets or not files_changed:
        return []
    files_value = shlex.quote(" ".join(files_changed))
    commands: list[str] = []
    if "format" in targets:
        commands.append(f"make format FILES={files_value}")
    elif "fmt" in targets:
        commands.append(f"make fmt FILES={files_value}")
    if "lint-fix" in targets:
        commands.append(f"make lint-fix FILES={files_value}")
    elif "lint_fix" in targets:
        commands.append(f"make lint_fix FILES={files_value}")
    elif "lint" in targets:
        commands.append(f"make lint FILES={files_value}")
    return commands


def detect_post_run_lint_commands(
    *,
    repo_path: Path,
    files_changed: Sequence[str],
    fallback_command_template: str | None = None,
) -> list[str]:
    """Detect post-run lint/format commands constrained to changed files."""
    if not files_changed:
        return []

    python_files = _python_files_only(files_changed)
    if python_files and _ruff_config_present(repo_path):
        python_args = _render_file_args(python_files)
        return [
            f"ruff format -- {python_args}",
            f"ruff check --fix -- {python_args}",
        ]

    package_commands = _package_json_lint_commands(
        repo_path=repo_path,
        files_changed=files_changed,
    )
    if package_commands:
        return package_commands

    make_commands = _makefile_lint_commands(
        repo_path=repo_path,
        files_changed=files_changed,
    )
    if make_commands:
        return make_commands

    if fallback_command_template is None or not fallback_command_template.strip():
        return []

    template = fallback_command_template.strip()
    all_files = _render_file_args(files_changed)
    if _DEFAULT_FALLBACK_TEMPLATE_KEY in template:
        command = template.replace(_DEFAULT_FALLBACK_TEMPLATE_KEY, all_files)
    else:
        command = f"{template} {all_files}".strip()
    return [command]


def _stream_artifact_uri(
    result: DockerShellCommandResult,
    *,
    stream_name: str,
) -> str | None:
    for artifact in result.artifacts:
        if artifact.artifact_metadata.get("stream") == stream_name:
            return artifact.uri
    return None


def _artifact_references(
    result: DockerShellCommandResult,
    *,
    command_index: int,
) -> list[ArtifactReference]:
    refs: list[ArtifactReference] = []
    for artifact in result.artifacts:
        refs.append(
            ArtifactReference(
                name=f"post-run-lint-{command_index}-{artifact.name}",
                uri=artifact.uri,
                artifact_type=artifact.artifact_type,
            )
        )
    return refs


def run_post_run_lint(
    *,
    session: ShellSessionProtocol,
    repo_path_for_detection: Path,
    repo_working_directory: Path,
    files_changed: Sequence[str],
    timeout_seconds: int,
    fallback_command_template: str | None = None,
) -> dict[str, Any]:
    """Run scoped post-run lint/format commands and return verification metadata."""
    with start_optional_span(
        tracer_name="workers.post_run_lint",
        span_name="worker.post_run_lint",
        attributes={
            "code_agent.timeout_seconds": timeout_seconds,
            "code_agent.files_changed_count": len(files_changed),
        },
    ) as span:
        commands = detect_post_run_lint_commands(
            repo_path=repo_path_for_detection,
            files_changed=files_changed,
            fallback_command_template=fallback_command_template,
        )
        if span is not None:
            span.set_attribute("code_agent.commands_detected_count", len(commands))
        if not commands:
            return {
                "ran": False,
                "status": "skipped",
                "reason": "no_detected_lint_or_format_command",
                "commands": [],
                "errors": [],
                "artifacts": [],
            }

        executed_commands: list[WorkerCommand] = []
        artifact_refs: list[ArtifactReference] = []
        errors: list[str] = []

        for index, command in enumerate(commands):
            scoped_command = f"cd {shlex.quote(str(repo_working_directory))} && {command}"
            try:
                result = session.execute(scoped_command, timeout_seconds=timeout_seconds)
            except DockerShellSessionError as exc:
                logger.warning(
                    "Post-run lint command failed before completion",
                    extra={"command": command},
                )
                errors.append(f"post-run lint command failed to execute: {command}: {exc}")
                continue

            executed_commands.append(
                WorkerCommand(
                    command=command,
                    exit_code=result.exit_code,
                    duration_seconds=result.duration_seconds,
                    stdout_artifact_uri=_stream_artifact_uri(result, stream_name="stdout"),
                    stderr_artifact_uri=_stream_artifact_uri(result, stream_name="stderr"),
                )
            )
            artifact_refs.extend(_artifact_references(result, command_index=index))
            if result.exit_code != 0:
                errors.append(f"`{command}` exited with status {result.exit_code}")

        if span is not None:
            span.set_attribute("code_agent.commands_executed_count", len(executed_commands))
            span.set_attribute("code_agent.errors_count", len(errors))
            span.set_attribute(
                "code_agent.lint_status",
                "warning" if errors else "passed",
            )
        return {
            "ran": True,
            "status": "warning" if errors else "passed",
            "reason": None,
            "commands": [command.model_dump(mode="json") for command in executed_commands],
            "errors": errors,
            "artifacts": [artifact.model_dump(mode="json") for artifact in artifact_refs],
        }


def apply_post_run_lint_format(
    *,
    session: ShellSessionProtocol,
    execution: CliRuntimeExecutionResult,
    files_changed: list[str],
    repo_path_for_detection: Path,
    repo_working_directory: Path,
    timeout_seconds: int,
    fallback_command_template: str | None = None,
) -> tuple[list[str], dict[str, Any], list[ArtifactReference]]:
    """Run post-run lint/format and merge its effects into execution metadata."""
    lint_format_result = run_post_run_lint(
        session=session,
        repo_path_for_detection=repo_path_for_detection,
        repo_working_directory=repo_working_directory,
        files_changed=files_changed,
        timeout_seconds=timeout_seconds,
        fallback_command_template=fallback_command_template,
    )

    lint_format_commands = lint_format_result.get("commands")
    if isinstance(lint_format_commands, list) and lint_format_commands:
        execution.commands_run.extend(
            [
                WorkerCommand.model_validate(command)
                for command in lint_format_commands
                if isinstance(command, dict)
            ]
        )

    lint_format_artifacts_raw = lint_format_result.get("artifacts")
    lint_format_artifacts: list[ArtifactReference] = []
    if isinstance(lint_format_artifacts_raw, list):
        lint_format_artifacts = [
            ArtifactReference.model_validate(artifact)
            for artifact in lint_format_artifacts_raw
            if isinstance(artifact, dict)
        ]

    updated_files_changed = list(files_changed)
    if lint_format_result.get("ran"):
        refreshed_files_changed = _collect_changed_files_with_fallback(
            session=session,
            repo_working_directory=repo_working_directory,
            repo_path_for_detection=repo_path_for_detection,
            timeout_seconds=timeout_seconds,
        )
        if refreshed_files_changed:
            updated_files_changed = _merge_changed_files(
                updated_files_changed,
                refreshed_files_changed,
            )

    return updated_files_changed, lint_format_result, lint_format_artifacts


def collect_changed_files_and_apply_post_run_lint_format(
    *,
    session: ShellSessionProtocol,
    execution: CliRuntimeExecutionResult,
    expect_changed_files_artifact: bool,
    repo_path_for_detection: Path,
    repo_working_directory: Path,
    timeout_seconds: int,
    fallback_command_template: str | None = None,
    existing_files_changed: Sequence[str] | None = None,
) -> tuple[list[str], dict[str, Any], list[ArtifactReference]]:
    """Collect changed files (with fallback) and run post-run lint/format."""
    files_changed = list(existing_files_changed or [])
    if expect_changed_files_artifact:
        collected_files = _collect_changed_files_with_fallback(
            session=session,
            repo_working_directory=repo_working_directory,
            repo_path_for_detection=repo_path_for_detection,
            timeout_seconds=timeout_seconds,
        )
        if collected_files:
            files_changed = _merge_changed_files(files_changed, collected_files)

    return apply_post_run_lint_format(
        session=session,
        execution=execution,
        files_changed=files_changed,
        repo_path_for_detection=repo_path_for_detection,
        repo_working_directory=repo_working_directory,
        timeout_seconds=timeout_seconds,
        fallback_command_template=fallback_command_template,
    )
