"""Reusable one-shot native-agent CLI runner."""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from workers.base import ArtifactReference
from workers.cli_runtime import collect_changed_files_from_repo_path

logger = logging.getLogger(__name__)

DEFAULT_NATIVE_AGENT_TIMEOUT_SECONDS = 600
DEFAULT_NATIVE_AGENT_DIFF_TIMEOUT_SECONDS = 15
DEFAULT_NATIVE_AGENT_CHANGED_FILES_TIMEOUT_SECONDS = 10
DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR = ".code-agent/native-agent-runner"
_FINAL_MESSAGE_FIELDS = ("final_output", "summary", "message", "content")


@dataclass(frozen=True)
class NativeAgentRunRequest:
    """Inputs required for one native-agent CLI execution."""

    command: list[str]
    prompt: str
    repo_path: Path
    workspace_path: Path
    timeout_seconds: int = DEFAULT_NATIVE_AGENT_TIMEOUT_SECONDS
    diff_timeout_seconds: int = DEFAULT_NATIVE_AGENT_DIFF_TIMEOUT_SECONDS
    changed_files_timeout_seconds: int = DEFAULT_NATIVE_AGENT_CHANGED_FILES_TIMEOUT_SECONDS
    env: dict[str, str] | None = None
    final_message_path: Path | None = None
    events_path: Path | None = None
    collect_diff: bool = True
    collect_changed_files: bool = True


@dataclass
class NativeAgentRunResult:
    """Structured output from one native-agent CLI execution."""

    status: Literal["success", "failure", "error"]
    summary: str
    command: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    final_message: str | None = None
    diff_text: str | None = None
    files_changed: list[str] = field(default_factory=list)
    artifacts: list[ArtifactReference] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


def _normalize_stream_payload(payload: str | bytes | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def _read_final_message(path: Path) -> str | None:
    if not path.exists():
        return None
    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text

    if isinstance(payload, str):
        value = payload.strip()
        return value or None
    if isinstance(payload, dict):
        for field_name in _FINAL_MESSAGE_FIELDS:
            raw_value = payload.get(field_name)
            if isinstance(raw_value, str):
                normalized = raw_value.strip()
                if normalized:
                    return normalized
    return raw_text


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
        logger.warning(
            "Native agent runner git diff failed.",
            extra={"exit_code": completed.returncode},
        )
        return None
    payload = completed.stdout.strip()
    return payload or None


def run_native_agent(request: NativeAgentRunRequest) -> NativeAgentRunResult:
    """Run one native-agent CLI command and capture stable worker-facing outputs."""

    if not request.command:
        raise ValueError("NativeAgentRunRequest.command must include at least one argv token.")

    repo_path = request.repo_path.expanduser().resolve()
    workspace_path = request.workspace_path.expanduser().resolve()
    final_message_path = (
        request.final_message_path.expanduser().resolve()
        if request.final_message_path is not None
        else None
    )
    events_path = request.events_path.expanduser().resolve() if request.events_path else None
    artifact_root = (
        workspace_path
        / DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR
        / f"run-{int(time.time() * 1000)}-{time.monotonic_ns()}"
    )
    artifact_root.mkdir(parents=True, exist_ok=True)

    command_text = shlex.join(request.command)
    started_at = time.perf_counter()

    try:
        completed = subprocess.run(
            request.command,
            input=request.prompt,
            check=False,
            capture_output=True,
            text=True,
            cwd=repo_path,
            env=request.env,
            timeout=request.timeout_seconds,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started_at
        stdout_text = _normalize_stream_payload(exc.stdout)
        stderr_text = _normalize_stream_payload(exc.stderr)
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
        return NativeAgentRunResult(
            status="error",
            summary=f"Native agent command timed out after {request.timeout_seconds}s.",
            command=command_text,
            exit_code=None,
            duration_seconds=elapsed,
            timed_out=True,
            artifacts=artifacts,
            stdout=stdout_text,
            stderr=stderr_text,
        )
    except OSError as exc:
        elapsed = time.perf_counter() - started_at
        return NativeAgentRunResult(
            status="error",
            summary=f"Native agent command could not start `{request.command[0]}`: {exc}",
            command=command_text,
            exit_code=None,
            duration_seconds=elapsed,
            timed_out=False,
        )

    elapsed = time.perf_counter() - started_at
    timed_out = False
    stdout_text = completed.stdout or ""
    stderr_text = completed.stderr or ""
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

    final_message_artifact = (
        _copy_artifact(
            artifact_root=artifact_root,
            source_path=final_message_path,
            file_name="final-message.txt",
            name="native-agent-final-message",
            artifact_type="result_summary",
        )
        if final_message_path is not None
        else None
    )
    if final_message_artifact is not None:
        artifacts.append(final_message_artifact)

    final_message = _read_final_message(final_message_path) if final_message_path else None
    if final_message is None:
        maybe_stdout_message = stdout_text.strip()
        final_message = maybe_stdout_message or None

    files_changed = (
        collect_changed_files_from_repo_path(
            repo_path,
            timeout_seconds=request.changed_files_timeout_seconds,
        )
        if request.collect_changed_files
        else []
    )
    diff_text = (
        _collect_diff_text(repo_path=repo_path, timeout_seconds=request.diff_timeout_seconds)
        if request.collect_diff
        else None
    )
    if diff_text is not None:
        artifacts.append(
            _write_artifact(
                artifact_root=artifact_root,
                file_name="diff.patch",
                content=diff_text,
                name="native-agent-diff",
                artifact_type="diff",
            )
        )

    if completed.returncode == 0:
        summary = final_message or "Native agent run completed successfully."
        status: Literal["success", "failure", "error"] = "success"
    else:
        summary = f"Native agent command exited with code {completed.returncode}."
        status = "failure"

    return NativeAgentRunResult(
        status=status,
        summary=summary,
        command=command_text,
        exit_code=completed.returncode,
        duration_seconds=elapsed,
        timed_out=timed_out,
        final_message=final_message,
        diff_text=diff_text,
        files_changed=files_changed,
        artifacts=artifacts,
        stdout=stdout_text,
        stderr=stderr_text,
    )
