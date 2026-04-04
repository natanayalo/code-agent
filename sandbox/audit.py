"""Shared audit and artifact capture utilities for sandbox runners."""

from __future__ import annotations

import logging
import os
import subprocess
import typing
from pathlib import Path
from uuid import uuid4

from sandbox.redact import SecretRedactor
from sandbox.workspace import SandboxArtifact, WorkspaceHandle

logger = logging.getLogger(__name__)

_ARTIFACT_ROOT_DIR = "artifacts"
_ARTIFACT_RUN_DIR_PREFIX = "command-"


def run_git_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    """Run a git inspection command against the workspace repo."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        timeout=timeout,
    )


def create_artifact_directory(workspace: WorkspaceHandle) -> Path:
    """Create a unique artifact directory for one sandbox command result."""
    artifact_dir = workspace.workspace_path / _ARTIFACT_ROOT_DIR
    artifact_dir = artifact_dir / f"{_ARTIFACT_RUN_DIR_PREFIX}{uuid4().hex[:8]}"
    artifact_dir.mkdir(parents=True, exist_ok=False)
    return artifact_dir


def write_text_artifact(
    workspace: WorkspaceHandle,
    artifact_dir: Path,
    *,
    filename: str,
    content: str,
    artifact_type: str,
    artifact_metadata: dict[str, typing.Any] | None = None,
) -> SandboxArtifact:
    """Persist a text artifact under the workspace artifact directory."""
    artifact_path = artifact_dir / filename
    artifact_path.write_text(content, encoding="utf-8")
    return SandboxArtifact(
        name=filename,
        uri=str(artifact_path.relative_to(workspace.workspace_path)),
        artifact_type=artifact_type,
        artifact_metadata=artifact_metadata or {},
    )


def parse_git_status_entries(status_output: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain=v1 -z` output into `(status, path)` tuples."""
    entries: list[tuple[str, str]] = []
    tokens = iter(status_output.split("\0"))
    for token in tokens:
        if not token:
            continue

        status = token[:2]
        path = token[3:]

        if "R" in status or "C" in status:
            new_path = next(tokens, "")
            if new_path:
                path = new_path

        entries.append((status, path))

    return entries


def format_changed_files(files_changed: list[str]) -> str:
    """Render the changed-file snapshot as a readable text artifact."""
    if not files_changed:
        return "No changed files detected.\n"
    return "".join(f"{path}\n" for path in files_changed)


def build_diff_summary(
    repo_path: Path,
    *,
    untracked_files: list[str],
) -> str | None:
    """Build an optional diff summary artifact for tracked and untracked changes."""
    sections: list[str] = []
    head_check = run_git_command(["git", "rev-parse", "--verify", "HEAD"], cwd=repo_path)
    if head_check.returncode == 0:
        tracked_diff = run_git_command(
            ["git", "diff", "--stat", "--summary", "HEAD", "--"],
            cwd=repo_path,
        )
        if tracked_diff.returncode == 0:
            tracked_summary = tracked_diff.stdout.decode("utf-8", errors="replace").strip()
            if tracked_summary:
                sections.append(tracked_summary)
        else:
            logger.warning(
                "Failed to collect sandbox diff summary",
                extra={
                    "repo_path": str(repo_path),
                    "stderr": tracked_diff.stderr.decode("utf-8", errors="replace").strip(),
                },
            )

    if untracked_files:
        sections.append(
            "Untracked files:\n" + "".join(f"- {path}\n" for path in untracked_files).rstrip()
        )

    summary = "\n\n".join(section for section in sections if section).strip()
    return summary or None


def capture_audit_artifacts(
    workspace: WorkspaceHandle,
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    redactor: SecretRedactor | None = None,
) -> tuple[list[str], list[SandboxArtifact]]:
    """Persist command artifacts and snapshot workspace changes."""
    artifact_dir = create_artifact_directory(workspace)
    artifacts = [
        write_text_artifact(
            workspace,
            artifact_dir,
            filename="stdout.log",
            content=redactor.redact(stdout) if redactor else stdout,
            artifact_type="log",
            artifact_metadata={"stream": "stdout", "exit_code": exit_code},
        ),
        write_text_artifact(
            workspace,
            artifact_dir,
            filename="stderr.log",
            content=redactor.redact(stderr) if redactor else stderr,
            artifact_type="log",
            artifact_metadata={"stream": "stderr", "exit_code": exit_code},
        ),
    ]

    files_changed: list[str] = []
    try:
        status_result = run_git_command(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=workspace.repo_path,
        )
        if status_result.returncode != 0:
            raise RuntimeError(
                status_result.stderr.decode("utf-8", errors="replace").strip()
                or "git status failed without output"
            )

        status_entries = parse_git_status_entries(
            status_result.stdout.decode("utf-8", errors="replace")
        )
        files_changed = list(dict.fromkeys(path for _status, path in status_entries))
        untracked_files = [path for status, path in status_entries if status == "??"]
        artifacts.append(
            write_text_artifact(
                workspace,
                artifact_dir,
                filename="changed-files.txt",
                content=format_changed_files(files_changed),
                artifact_type="result_summary",
                artifact_metadata={
                    "kind": "changed_files",
                    "files_changed_count": len(files_changed),
                },
            )
        )

        diff_summary = build_diff_summary(
            workspace.repo_path,
            untracked_files=untracked_files,
        )
        if diff_summary:
            artifacts.append(
                write_text_artifact(
                    workspace,
                    artifact_dir,
                    filename="diff-summary.txt",
                    content=diff_summary + "\n",
                    artifact_type="diff",
                    artifact_metadata={
                        "kind": "diff_summary",
                        "files_changed_count": len(files_changed),
                    },
                )
            )
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "Failed to inspect sandbox workspace changes",
            extra={
                "workspace_id": workspace.workspace_id,
                "task_id": workspace.task_id,
                "error": str(exc),
            },
        )
        artifacts.append(
            write_text_artifact(
                workspace,
                artifact_dir,
                filename="changed-files.txt",
                content=f"Failed to inspect workspace changes: {exc}\n",
                artifact_type="result_summary",
                artifact_metadata={"kind": "changed_files", "inspection_error": str(exc)},
            )
        )

    return files_changed, artifacts
