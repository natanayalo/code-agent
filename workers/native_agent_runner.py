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
from typing import Final, Literal

from apps.observability import (
    NATIVE_AGENT_COMMAND_ATTRIBUTE,
    NATIVE_AGENT_DURATION_ATTRIBUTE,
    NATIVE_AGENT_EXIT_CODE_ATTRIBUTE,
    NATIVE_AGENT_STDERR_ATTRIBUTE,
    NATIVE_AGENT_STDOUT_ATTRIBUTE,
    NATIVE_AGENT_TIMED_OUT_ATTRIBUTE,
    SPAN_KIND_AGENT,
    set_current_span_attribute,
    set_span_input_output,
    set_span_status_from_outcome,
    start_optional_span,
    with_span_kind,
)
from sandbox.redact import SecretRedactor, redact_and_truncate_output, sanitize_command
from workers.adapter_utils import truncate_detail_keep_tail
from workers.base import ArtifactReference
from workers.cli_runtime import collect_changed_files_from_repo_path

logger = logging.getLogger(__name__)

DEFAULT_NATIVE_AGENT_TIMEOUT_SECONDS = 600
DEFAULT_NATIVE_AGENT_DIFF_TIMEOUT_SECONDS = 15
DEFAULT_NATIVE_AGENT_CHANGED_FILES_TIMEOUT_SECONDS = 10
DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR = ".code-agent/native-agent-runner"
DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS = 64 * 1024
DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS = 1000
_STDOUT_FALLBACK_TRUNCATION_NOTE = "[stdout truncated for summary]\n"
NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH: Final[int] = 2000
_FINAL_MESSAGE_FIELDS: Final = (
    "error",
    "final_output",
    "summary",
    "message",
    "content",
    "response",
)


def _extract_final_message(raw_text: str) -> str | None:
    """Extract a meaningful final message from raw text or JSON."""
    candidate = raw_text.strip()
    if not candidate:
        return None

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        return candidate

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

            # Handle structured error payloads (parity with previous GeminiCliWorker logic)
            if field_name == "error" and isinstance(raw_value, dict):
                err_type = raw_value.get("type")
                err_msg = raw_value.get("message")
                if isinstance(err_type, str) and isinstance(err_msg, str):
                    return f"{err_type}: {err_msg}"
                if isinstance(err_msg, str):
                    return err_msg
                if isinstance(err_type, str):
                    return err_type

    return candidate


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
    redactor: SecretRedactor | None = None


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


def format_native_run_summary(result: NativeAgentRunResult) -> str:
    """Format a human-readable summary from a native agent run result."""
    base = result.final_message or result.summary
    if result.status == "success":
        return base

    # Include truncated stderr for failures to aid classification and debugging
    stderr_preview = truncate_detail_keep_tail(result.stderr, max_characters=500)
    return f"{base} {stderr_preview}".strip()


def _normalize_stream_payload(payload: str | bytes | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def _read_final_message(path: Path) -> str | None:
    """Read and parse the final message from a file."""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        raw_payload = handle.read(DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS + 1)
    truncated = len(raw_payload) > DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS
    raw_text = raw_payload[:DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS].strip()
    if not raw_text:
        return None

    extracted = _extract_final_message(raw_text)
    if extracted and truncated:
        return f"{extracted}\n\n[final message truncated for safety]"
    return extracted


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
        stderr_preview = (completed.stderr or "").strip()
        logger.warning(
            "Native agent runner git diff failed.",
            extra={"exit_code": completed.returncode, "stderr": stderr_preview},
        )
        return None
    payload = completed.stdout.strip()
    return payload or None


def _stdout_fallback_final_message(stdout_text: str) -> str | None:
    """Extract final message from stdout tail, prioritizing JSON extraction from the end."""
    candidate = stdout_text.strip()
    if not candidate:
        return None

    # Limit search space to avoid parsing giant outputs
    search_limit = DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS
    is_truncated = len(candidate) > search_limit
    search_space = candidate[-search_limit:] if is_truncated else candidate

    # 1. Try parsing the search space as a whole (could be a full JSON response)
    extracted = _extract_final_message(search_space)
    if extracted and extracted != search_space:
        return extracted

    # 2. Try finding a JSON block in the search space (could be logs followed by JSON)
    # We iterate backwards and use raw_decode to find the last valid JSON object.
    decoder = json.JSONDecoder()
    pos = search_space.rfind("{")
    while pos != -1:
        try:
            _, end_idx = decoder.raw_decode(search_space[pos:])
            block = search_space[pos : pos + end_idx]
            extracted = _extract_final_message(block)
            if extracted and extracted != block:
                return extracted
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug("Failed to decode JSON block at position %d: %s", pos, e)
        pos = search_space.rfind("{", 0, pos)

    # 3. Fallback to raw text (with truncation note if applicable)
    if is_truncated:
        return f"{_STDOUT_FALLBACK_TRUNCATION_NOTE}{search_space}"
    return extracted or search_space


def _finalize_native_agent_run(
    request: NativeAgentRunRequest,
    *,
    status: Literal["success", "failure", "error"],
    summary: str,
    command_text: str,
    started_at: float,
    timed_out: bool,
    exit_code: int | None = None,
    stdout: str = "",
    stderr: str = "",
    final_message: str | None = None,
    diff_text: str | None = None,
    files_changed: list[str] | None = None,
    artifacts: list[ArtifactReference] | None = None,
) -> NativeAgentRunResult:
    """Centralize NativeAgentRunResult construction and standardized span metadata recording."""
    elapsed = time.perf_counter() - started_at
    result = NativeAgentRunResult(
        status=status,
        summary=summary,
        command=command_text,
        exit_code=exit_code,
        duration_seconds=elapsed,
        timed_out=timed_out,
        final_message=final_message,
        diff_text=diff_text,
        files_changed=files_changed or [],
        artifacts=artifacts or [],
        stdout=stdout,
        stderr=stderr,
    )

    # Standardized Tracing Metadata
    set_span_input_output(
        input_data=None,  # Input data is recorded at the start of run_native_agent span
        output_data=redact_and_truncate_output(summary, redactor=request.redactor),
    )
    set_current_span_attribute(NATIVE_AGENT_EXIT_CODE_ATTRIBUTE, result.exit_code)
    set_current_span_attribute(NATIVE_AGENT_TIMED_OUT_ATTRIBUTE, timed_out)
    set_current_span_attribute(NATIVE_AGENT_DURATION_ATTRIBUTE, elapsed)
    set_current_span_attribute(
        NATIVE_AGENT_STDOUT_ATTRIBUTE,
        redact_and_truncate_output(
            stdout, redactor=request.redactor, limit_chars=NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH
        ),
    )
    set_current_span_attribute(
        NATIVE_AGENT_STDERR_ATTRIBUTE,
        redact_and_truncate_output(
            stderr, redactor=request.redactor, limit_chars=NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH
        ),
    )
    set_span_status_from_outcome(result.status, result.summary)

    return result


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
    completed: subprocess.CompletedProcess | None = None
    stdout_text: str = ""
    stderr_text: str = ""

    with start_optional_span(
        tracer_name="workers.native_agent_runner",
        span_name="native_agent_run",
        attributes=with_span_kind(SPAN_KIND_AGENT),
    ):
        set_span_input_output(
            input_data=redact_and_truncate_output(request.prompt, redactor=request.redactor),
        )
        set_current_span_attribute(
            NATIVE_AGENT_COMMAND_ATTRIBUTE, sanitize_command(command_text, request.redactor)
        )

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
            stdout_text = _normalize_stream_payload(exc.stdout)
            stderr_text = _normalize_stream_payload(exc.stderr)
            artifacts: list[ArtifactReference] = []
            try:
                artifacts.extend(
                    [
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
                )
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
            except Exception:
                logger.exception(
                    "Native agent runner failed while collecting timeout artifacts.",
                    extra={"command": command_text},
                )

            return _finalize_native_agent_run(
                request=request,
                status="error",
                summary=f"Native agent command timed out after {request.timeout_seconds}s.",
                command_text=command_text,
                started_at=started_at,
                timed_out=True,
                stdout=stdout_text,
                stderr=stderr_text,
                artifacts=artifacts,
            )
        except OSError as exc:
            return _finalize_native_agent_run(
                request=request,
                status="error",
                summary=(f"Native agent command could not start `{request.command[0]}`: {exc}"),
                command_text=command_text,
                started_at=started_at,
                timed_out=False,
            )

        timed_out = False
        stdout_text = completed.stdout or ""
        stderr_text = completed.stderr or ""
        try:
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
                final_message = _stdout_fallback_final_message(stdout_text)

            files_changed = (
                collect_changed_files_from_repo_path(
                    repo_path,
                    timeout_seconds=request.changed_files_timeout_seconds,
                )
                if request.collect_changed_files
                else []
            )
            diff_text = (
                _collect_diff_text(
                    repo_path=repo_path, timeout_seconds=request.diff_timeout_seconds
                )
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

            return _finalize_native_agent_run(
                request=request,
                status=status,
                summary=summary,
                command_text=command_text,
                started_at=started_at,
                timed_out=timed_out,
                exit_code=completed.returncode,
                stdout=stdout_text,
                stderr=stderr_text,
                final_message=final_message,
                diff_text=diff_text,
                files_changed=files_changed,
                artifacts=artifacts,
            )
        except Exception as exc:
            logger.exception(
                "Native agent runner failed while collecting artifacts or metadata.",
                extra={
                    "command": command_text,
                    "exit_code": completed.returncode if completed else None,
                },
            )
            return _finalize_native_agent_run(
                request=request,
                status="error",
                summary=f"Native agent runner failed while collecting artifacts: {exc}",
                command_text=command_text,
                started_at=started_at,
                timed_out=False,
                exit_code=completed.returncode if completed else None,
                stdout=stdout_text,
                stderr=stderr_text,
            )
