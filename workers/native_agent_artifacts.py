from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR = ".code-agent/native-agent-runner"


from workers.base import ArtifactReference  # noqa: E402

logger = logging.getLogger(__name__)


def _write_artifact(
    *,
    artifact_root: Path,
    file_name: str,
    content: str,
    name: str,
    artifact_type: str | None = None,
) -> ArtifactReference:
    path = artifact_root / file_name
    path.write_text(content, encoding="utf-8")
    return ArtifactReference(
        name=name,
        uri=path.as_uri(),
        artifact_type=artifact_type,
    )


def _copy_artifact(
    *,
    artifact_root: Path,
    source_path: Path,
    file_name: str,
    name: str,
    artifact_type: str | None = None,
) -> ArtifactReference | None:
    if not source_path.exists():
        return None
    target_path = artifact_root / file_name
    shutil.copy2(source_path, target_path)
    return ArtifactReference(
        name=name,
        uri=target_path.as_uri(),
        artifact_type=artifact_type,
    )


def _collect_standard_artifacts(
    *,
    artifact_root: Path,
    stdout_text: str,
    stderr_text: str,
    events_path: Path | None,
) -> list[ArtifactReference]:
    """Write and return the standard set of execution artifacts."""
    artifacts = [
        _write_artifact(
            artifact_root=artifact_root,
            file_name="stdout.txt",
            content=stdout_text,
            name="native-agent-stdout",
            artifact_type="log",
        ),
        _write_artifact(
            artifact_root=artifact_root,
            file_name="stderr.txt",
            content=stderr_text,
            name="native-agent-stderr",
            artifact_type="log",
        ),
    ]

    event_artifact = (
        _copy_artifact(
            artifact_root=artifact_root,
            source_path=events_path,
            file_name="events.jsonl",
            name="native-agent-events",
            artifact_type="log",
        )
        if events_path is not None
        else None
    )
    if event_artifact is not None:
        artifacts.append(event_artifact)

    return artifacts


def _collect_diff_text(*, repo_path: Path, timeout_seconds: int) -> str | None:
    command = ["git", "-C", str(repo_path), "diff", "--no-color", "--", "."]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("Native agent runner failed to collect git diff.", exc_info=True)
        return None

    if completed.returncode != 0:
        stderr_preview = (completed.stderr or "").strip()
        logger.warning(
            "Native agent runner git diff failed.",
            extra={"exit_code": completed.returncode, "stderr": stderr_preview},
        )
        return None
    payload = completed.stdout.strip()
    return payload or None


def _collect_diff_text_since_ref(
    *,
    repo_path: Path,
    base_ref: str | None,
    timeout_seconds: int,
) -> str | None:
    """Collect a patch from the starting git ref plus any working-tree edits."""
    command = [
        "git",
        "-C",
        str(repo_path),
        "diff",
        "--no-color",
    ]
    if base_ref:
        command.append(base_ref)
    command.extend(["--", "."])

    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning(
            "Native agent runner failed to collect git diff.",
            exc_info=True,
        )
        return None

    if completed.returncode != 0:
        stderr_preview = (completed.stderr or "").strip()
        logger.warning(
            "Native agent runner git diff failed.",
            extra={"exit_code": completed.returncode, "stderr": stderr_preview},
        )
        return None

    return completed.stdout.strip() or None
