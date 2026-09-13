from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from sandbox.redact import REDACTED_OUTPUT_LIMIT, SecretRedactor, redact_and_truncate_output
from workers.base import ArtifactReference

DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR = ".code-agent/native-agent-runner"

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


def _copy_redacted_log_artifact(
    *,
    artifact_root: Path,
    source_path: Path,
    file_name: str,
    name: str,
    redactor: SecretRedactor | None,
) -> ArtifactReference | None:
    """Persist a bounded, redacted provider log when the CLI produced one."""
    try:
        with source_path.open(encoding="utf-8", errors="replace") as source_file:
            content = source_file.read(REDACTED_OUTPUT_LIMIT + 1)
    except OSError:
        return None
    return _write_artifact(
        artifact_root=artifact_root,
        file_name=file_name,
        content=redact_and_truncate_output(content, redactor=redactor),
        name=name,
        artifact_type="log",
    )


def _scrub_reasoning(text: str) -> str:
    """Scrub reasoning / chain-of-thought blocks from stream output."""
    if not text:
        return ""
    cleaned = re.sub(
        r"<(thought|reasoning)>[\s\S]*?</\1>", "[REASONING REDACTED]", text, flags=re.IGNORECASE
    )
    if "{" in cleaned and ("reasoning" in cleaned or "thought" in cleaned):
        lines = []
        for line in cleaned.splitlines():
            line_str = line.strip()
            if line_str.startswith("{") and line_str.endswith("}"):
                try:
                    payload = json.loads(line_str)
                    if isinstance(payload, dict):
                        event_type = payload.get("type") or payload.get("event")
                        if event_type == "reasoning":
                            payload["content"] = "[REASONING REDACTED]"
                            if "text" in payload:
                                payload["text"] = "[REASONING REDACTED]"
                            if "delta" in payload:
                                payload["delta"] = "[REASONING REDACTED]"
                            line = json.dumps(payload)
                        item = payload.get("item")
                        if isinstance(item, dict) and item.get("type") == "reasoning":
                            item["content"] = "[REASONING REDACTED]"
                            if "text" in item:
                                item["text"] = "[REASONING REDACTED]"
                            payload["item"] = item
                            line = json.dumps(payload)
                except (ValueError, json.JSONDecodeError):
                    pass
            lines.append(line)
        cleaned = "\n".join(lines)
    return cleaned


def _collect_standard_artifacts(
    *,
    artifact_root: Path,
    stdout_text: str,
    stderr_text: str,
    events_path: Path | None,
    provider_log_path: Path | None,
    redactor: SecretRedactor | None,
) -> list[ArtifactReference]:
    """Write and return the standard set of execution artifacts."""
    clean_stdout = redact_and_truncate_output(_scrub_reasoning(stdout_text), redactor=redactor)
    clean_stderr = redact_and_truncate_output(_scrub_reasoning(stderr_text), redactor=redactor)
    artifacts = [
        _write_artifact(
            artifact_root=artifact_root,
            file_name="stdout.txt",
            content=clean_stdout,
            name="native-agent-stdout",
            artifact_type="log",
        ),
        _write_artifact(
            artifact_root=artifact_root,
            file_name="stderr.txt",
            content=clean_stderr,
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

    provider_log_artifact = (
        _copy_redacted_log_artifact(
            artifact_root=artifact_root,
            source_path=provider_log_path,
            file_name="provider.log",
            name="native-agent-provider-log",
            redactor=redactor,
        )
        if provider_log_path is not None
        else None
    )
    if provider_log_artifact is not None:
        artifacts.append(provider_log_artifact)

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
