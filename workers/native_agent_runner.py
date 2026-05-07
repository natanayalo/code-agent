"""Reusable one-shot native-agent CLI runner."""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from apps.observability import (
    SPAN_KIND_AGENT,
    inject_w3c_trace_context_env,
    set_span_input_output,
    set_span_status_from_outcome,
    start_optional_span,
    with_span_kind,
)
from workers.base import ArtifactReference
from workers.cli_runtime import collect_changed_files_from_repo_path
from workers.markdown import unwrap_markdown_json_fence

logger = logging.getLogger(__name__)

DEFAULT_NATIVE_AGENT_TIMEOUT_SECONDS = 600
DEFAULT_NATIVE_AGENT_DIFF_TIMEOUT_SECONDS = 15
DEFAULT_NATIVE_AGENT_CHANGED_FILES_TIMEOUT_SECONDS = 10
DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR = ".code-agent/native-agent-runner"
DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS = 64 * 1024
DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS = 32000
_STDOUT_FALLBACK_TRUNCATION_NOTE = "[stdout truncated for summary]\n"
_FINAL_MESSAGE_FIELDS = ("final_output", "summary", "message", "content")

# Patterns that indicate environmental or configuration blockers that should be treated as failures
# even if the process exits with code 0.
_BLOCKED_STATUS_PATTERNS = [
    r"bwrap: No permissions to create a new namespace",
    r"writing is blocked by read-only sandbox",
    r"rejected by user approval settings",
    r"No permissions to create a new namespace",
]


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
    span_name: str | None = None
    span_attributes: dict[str, Any] | None = None


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
    json_payload: dict[str, Any] | None = None
    token_counts: dict[str, int] | None = None


def _normalize_stream_payload(payload: str | bytes | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return payload


def _read_final_message(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        raw_payload = handle.read(DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS + 1)
    truncated = len(raw_payload) > DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS
    raw_text = raw_payload[:DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS].strip()
    if not raw_text:
        return None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        if truncated:
            return f"{raw_text}\n\n[final message truncated for safety]"
        return raw_text

    if isinstance(payload, str):
        value = payload.strip()
        return value or None, None
    if isinstance(payload, dict):
        summary = None
        for field_name in _FINAL_MESSAGE_FIELDS:
            raw_value = payload.get(field_name)
            if isinstance(raw_value, str):
                normalized = raw_value.strip()
                if normalized:
                    summary = normalized
                    break
        return summary, payload
    return None, None
    if truncated:
        return f"{raw_text}\n\n[final message truncated for safety]"
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
        stderr_preview = (completed.stderr or "").strip()
        logger.warning(
            "Native agent runner git diff failed.",
            extra={"exit_code": completed.returncode, "stderr": stderr_preview},
        )
        return None
    payload = completed.stdout.strip()
    return payload or None


def _stdout_fallback_final_message(stdout_text: str) -> str | None:
    candidate = stdout_text.strip()
    if not candidate:
        return None

    # Try to extract a clean JSON block if it exists
    unwrapped = unwrap_markdown_json_fence(candidate)
    if unwrapped != candidate:
        try:
            json.loads(unwrapped)
            return unwrapped
        except json.JSONDecodeError:
            pass

    if len(candidate) <= DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS:
        return candidate
    tail = candidate[-DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS:]
    return f"{_STDOUT_FALLBACK_TRUNCATION_NOTE}{tail}"


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
    env = inject_w3c_trace_context_env(request.env)

    with start_optional_span(
        tracer_name=__name__,
        span_name=request.span_name or f"native_agent_run:{request.command[0]}",
        attributes=with_span_kind(
            SPAN_KIND_AGENT,
            {
                "command": command_text,
                "repo_path": str(repo_path),
                "timeout_seconds": request.timeout_seconds,
                **(request.span_attributes or {}),
            },
        ),
    ):
        set_span_input_output(input_data=request.prompt)
        try:
            completed = subprocess.run(
                request.command,
                input=request.prompt,
                check=False,
                capture_output=True,
                text=True,
                cwd=repo_path,
                env=env,
                timeout=request.timeout_seconds,
            )
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started_at
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

            if final_message_path is not None:
                final_message, full_payload = _read_final_message(final_message_path)
            else:
                final_message, full_payload = None, None

            if final_message is None:
                final_message = _stdout_fallback_final_message(stdout_text)

            # Extract token counts if available in the structured payload
            token_counts = None
            if full_payload and "tokens" in full_payload:
                raw_tokens = full_payload["tokens"]
                if isinstance(raw_tokens, dict):
                    token_counts = {k: int(v) for k, v in raw_tokens.items() if str(v).isdigit()}

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

                # Re-evaluate status based on blocked patterns in output
                combined_output = f"{stdout_text}\n{stderr_text}"
                for pattern in _BLOCKED_STATUS_PATTERNS:
                    if re.search(pattern, combined_output, re.IGNORECASE):
                        status = "failure"
                        summary = f"Native agent was blocked by environment constraints: {pattern}"
                        break
            else:
                summary = f"Native agent command exited with code {completed.returncode}."
                status = "failure"

            if token_counts:
                # Map standardized OpenInference token attributes
                set_current_span_attribute("llm.token_count.prompt", token_counts.get("input"))
                set_current_span_attribute("llm.token_count.completion", token_counts.get("output"))
                set_current_span_attribute("llm.token_count.total", token_counts.get("total"))

            set_span_status_from_outcome(status, summary)

            # Provide a rich structured output for the span
            span_output = {
                "status": status,
                "summary": summary,
                "exit_code": completed.returncode,
                "files_changed": len(files_changed),
                "duration_seconds": round(elapsed, 2),
                "token_counts": token_counts,
            }

            # If the summary or stdout contains valid JSON, prioritize it and potentially
            # suppress the full stdout_preview to keep traces clean (Point 1).
            json_found = False
            for text_source in [summary, stdout_text]:
                if not text_source:
                    continue
                unwrapped = unwrap_markdown_json_fence(text_source)
                try:
                    parsed = json.loads(unwrapped)
                    span_output["json_output"] = parsed
                    json_found = True
                    break
                except (json.JSONDecodeError, TypeError):
                    continue

            # Only include stdout preview if it is not excessively large and we didn't find clean JSON
            if stdout_text and not json_found:
                span_output["stdout_preview"] = stdout_text[:32000]

            set_span_input_output(input_data=None, output_data=span_output)

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
                json_payload=span_output.get("json_output"),
                token_counts=token_counts,
            )
        except Exception as exc:
            logger.exception(
                "Native agent runner failed while collecting artifacts or metadata.",
                extra={"command": command_text, "exit_code": completed.returncode},
            )
            return NativeAgentRunResult(
                status="error",
                summary=f"Native agent runner failed while collecting artifacts: {exc}",
                command=command_text,
                exit_code=completed.returncode,
                duration_seconds=elapsed,
                timed_out=False,
                stdout=stdout_text,
                stderr=stderr_text,
            )
