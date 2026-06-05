"""Reusable one-shot native-agent CLI runner."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from apps.observability import (
    DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS,
    DEFAULT_FINAL_MESSAGE_READ_BUFFER,
    NATIVE_AGENT_COMMAND_ATTRIBUTE,
    NATIVE_AGENT_DURATION_ATTRIBUTE,
    NATIVE_AGENT_EXIT_CODE_ATTRIBUTE,
    NATIVE_AGENT_STDERR_ATTRIBUTE,
    NATIVE_AGENT_STDOUT_ATTRIBUTE,
    NATIVE_AGENT_TIMED_OUT_ATTRIBUTE,
    NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH,
    SPAN_KIND_AGENT,
    SPAN_KIND_LLM,
    add_current_span_event,
    inject_w3c_trace_context_env,
    set_current_span_attribute,
    set_span_input_output,
    set_span_status_from_outcome,
    start_optional_span,
    with_span_kind,
)
from sandbox.redact import SecretRedactor, redact_and_truncate_output, sanitize_command
from workers.adapter_utils import format_native_run_summary, truncate_detail_keep_tail
from workers.base import ArtifactReference
from workers.cli_runtime import collect_changed_files_from_repo_path
from workers.constants import (
    DEFAULT_CHANGED_FILES_TIMEOUT_SECONDS as DEFAULT_NATIVE_AGENT_CHANGED_FILES_TIMEOUT_SECONDS,
)
from workers.constants import (
    DEFAULT_DIFF_TIMEOUT_SECONDS as DEFAULT_NATIVE_AGENT_DIFF_TIMEOUT_SECONDS,
)
from workers.constants import (
    DEFAULT_WORKER_TIMEOUT_SECONDS as DEFAULT_NATIVE_AGENT_TIMEOUT_SECONDS,
)
from workers.failure_taxonomy import find_infra_failure_marker
from workers.llm_tracing import set_llm_span_output, with_llm_span
from workers.native_agent_models import NativeAgentRunResult

logger = logging.getLogger(__name__)
_JSON_DECODER = json.JSONDecoder()

DEFAULT_NATIVE_AGENT_ARTIFACTS_DIR = ".code-agent/native-agent-runner"
DEFAULT_STDOUT_FALLBACK_FINAL_MESSAGE_MAX_CHARACTERS = 1000
_STDOUT_FALLBACK_TRUNCATION_NOTE = "[stdout truncated for summary]\n"
_FINAL_MESSAGE_FIELDS: Final = (
    "error",
    "final_output",
    "summary",
    "message",
    "content",
    "response",
)
_LLM_WRAPPER_PAYLOAD_KEYS: Final[tuple[str, ...]] = ("response", "content", "summary")
_LLM_METADATA_ATTR_PREFIX: Final[str] = "code_agent.native.llm_wrapper"
_JSON_PAYLOAD_ATTR_PREFIX: Final[str] = "code_agent.native.json_payload"
_LLM_WRAPPER_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {"session_id", "stats", "models", "tools", "files"}
)
_TELEMETRY_ONLY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api",
        "byName",
        "cached",
        "candidates",
        "files",
        "input",
        "models",
        "prompt",
        "roles",
        "stats",
        "thoughts",
        "tokens",
        "tool",
        "tools",
        "total",
        "totalCalls",
        "totalDecisions",
        "totalDurationMs",
        "totalErrors",
        "totalFail",
        "totalLatencyMs",
        "totalRequests",
        "totalSuccess",
    }
)
_FENCED_JSON_BLOCK_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"```(?:json)?\s*(?P<body>.*?)```",
    re.IGNORECASE | re.DOTALL,
)

# Standardized system environment variables that are safe to propagate to the sandbox.
SAFE_SYSTEM_ENV_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "TERM",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PIP_CERT",
        "SSL_CERT_FILE",
        "GPG_KEY",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PWD",
        "USER",
        "HOSTNAME",
    }
)

# Environment variable prefixes that must never be passed to the sandbox
# unless explicitly overridden.
SENSITIVE_ENV_PREFIX_DENYLIST: Final[frozenset[str]] = frozenset(
    {
        "AWS_",
        "GCP_",
        "OTEL_",
        "PHOENIX_",
        "DATABASE_",
        "POSTGRES_",
        "TELEGRAM_",
        "GEMINI_",
        "OPENROUTER_",
        "CODE_AGENT_",
    }
)

# Explicit auth-home keys that are safe to propagate even when HOME is isolated.
ALLOWED_AUTH_HOME_KEYS: Final[frozenset[str]] = frozenset({"CODEX_HOME", "GEMINI_HOME"})


_SIGNAL_EXIT_CODES: Final = {
    132: "SIGILL",
    -4: "SIGILL",
    134: "SIGABRT",
    -6: "SIGABRT",
    137: "SIGKILL",
    -9: "SIGKILL",
    135: "SIGBUS",
    138: "SIGBUS",
    -7: "SIGBUS",
    -10: "SIGBUS",
    139: "SIGSEGV",
    -11: "SIGSEGV",
}


def _detect_reason_code(
    *,
    status: Literal["success", "failure", "error"],
    timed_out: bool,
    exit_code: int | None,
    summary: str,
    stderr: str,
) -> tuple[str, str]:
    """Return stable reason_code/reason_detail for native run observability."""
    if timed_out:
        return "timeout", "command_timeout"
    if status == "success":
        return "ok", "completed"

    detail = summary.strip().lower()
    stderr_l = stderr.lower()
    if "auth method" in detail or "gemini_api_key" in stderr_l:
        return "auth_missing", "missing_auth_configuration"
    if "requires user confirmation" in detail:
        return "approval_blocked_noninteractive", "requires_user_confirmation"
    if "tool registry mismatch" in detail:
        return "tool_registry_mismatch", "tool_not_found"
    if "shell crash" in detail or "sandbox_infra" in detail:
        return "sandbox_infra_crash", "shell_crash_detected"
    if "could not start" in detail:
        return "process_start_failure", "process_could_not_start"
    if "failed while collecting artifacts" in detail:
        return "artifact_collection_failure", "artifact_collection_failed"
    if exit_code not in (None, 0):
        return "nonzero_exit", f"exit_code_{exit_code}"
    return "unknown_error", "unclassified_failure"


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
            if raw_value is None:
                continue

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

            if isinstance(raw_value, str):
                normalized = raw_value.strip()
                if normalized:
                    return normalized
            if isinstance(raw_value, dict):
                return json.dumps(raw_value)

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
    task_id: str | None = None
    session_id: str | None = None
    redactor: SecretRedactor | None = None
    response_format: Literal["text", "json"] = "text"
    response_schema: dict[str, Any] | None = None
    span_kind: str = SPAN_KIND_LLM


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
        raw_payload = handle.read(DEFAULT_FINAL_MESSAGE_READ_BUFFER)
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
    pos = search_space.rfind("{")
    while pos != -1:
        try:
            _, end_idx = _JSON_DECODER.raw_decode(search_space[pos:])
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


def _record_span_data(
    *,
    input_data: str | None = None,
    output_data: str | None = None,
    redactor: SecretRedactor | None = None,
) -> None:
    """Standardized recording of span input/output with redaction and truncation."""
    # Use consistent truncation limit for span attributes to prevent oversized payloads.
    limit = NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH
    redacted_input = (
        redact_and_truncate_output(input_data, redactor=redactor, limit_chars=limit)
        if input_data is not None
        else None
    )
    redacted_output = (
        redact_and_truncate_output(output_data, redactor=redactor, limit_chars=limit)
        if output_data is not None
        else None
    )
    set_span_input_output(input_data=redacted_input, output_data=redacted_output)


def _split_llm_output_and_metadata(raw_output: str) -> tuple[Any, dict[str, Any] | None]:
    """Return (payload_for_output_value, wrapper_metadata) for CLI JSON envelopes."""
    text = raw_output.strip()
    if not text:
        return raw_output, None

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw_output, None

    if not isinstance(parsed, dict):
        return parsed, None

    parsed_keys = set(parsed)
    payload_key = next(
        (
            key
            for key in _LLM_WRAPPER_PAYLOAD_KEYS
            if key in parsed
            and (key == "response" or bool(parsed_keys.intersection(_LLM_WRAPPER_METADATA_KEYS)))
        ),
        None,
    )
    if payload_key is None:
        return parsed, None

    payload = parsed.get(payload_key)
    if isinstance(payload, str):
        candidate = payload.strip()
        if candidate:
            try:
                payload = json.loads(candidate)
            except (json.JSONDecodeError, TypeError, ValueError):
                payload = payload
    metadata = {k: v for k, v in parsed.items() if k != payload_key}
    return payload, metadata or None


def _schema_property_names(response_schema: dict[str, Any] | None) -> frozenset[str]:
    if not response_schema:
        return frozenset()
    properties = response_schema.get("properties")
    if not isinstance(properties, dict):
        return frozenset()
    return frozenset(str(key) for key in properties)


def _json_payload_rejection_reason(
    payload: dict[str, Any],
    *,
    response_schema: dict[str, Any] | None,
    response_format: Literal["text", "json"],
) -> str | None:
    keys = set(payload)
    if keys and keys <= _TELEMETRY_ONLY_KEYS:
        return "telemetry_only"

    schema_keys = _schema_property_names(response_schema)
    if response_format == "json" and schema_keys and not keys.intersection(schema_keys):
        return "schema_key_mismatch"

    return None


def _parse_json_dict(text: str) -> dict[str, Any] | None:
    try:
        candidate = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return candidate if isinstance(candidate, dict) else None


def _parse_json_dict_from_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    return _parse_json_dict(value)


def _iter_fenced_json_dicts(text: str) -> list[dict[str, Any]]:
    return [
        payload
        for match in _FENCED_JSON_BLOCK_PATTERN.finditer(text)
        if (payload := _parse_json_dict(match.group("body"))) is not None
    ]


def _iter_embedded_json_dicts(text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    pos = text.find("{")
    while pos != -1:
        try:
            candidate, end_idx = _JSON_DECODER.raw_decode(text[pos:])
        except (json.JSONDecodeError, ValueError):
            pos = text.find("{", pos + 1)
            continue
        if isinstance(candidate, dict):
            payloads.append(candidate)
        pos = text.find("{", pos + max(end_idx, 1))
    return payloads


def _select_json_payload(
    candidates: list[tuple[str, dict[str, Any]]],
    *,
    response_schema: dict[str, Any] | None,
    response_format: Literal["text", "json"],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    rejected_reason: str | None = None
    for source, payload in candidates:
        rejection_reason = _json_payload_rejection_reason(
            payload,
            response_schema=response_schema,
            response_format=response_format,
        )
        if rejection_reason is None:
            return payload, source, None
        rejected_reason = rejected_reason or rejection_reason
    return None, None, rejected_reason


def _extract_business_json_payload(
    *,
    final_message: str | None,
    stdout_text: str,
    response_format: Literal["text", "json"],
    response_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Extract model business JSON while rejecting CLI telemetry envelopes."""
    candidates: list[tuple[str, dict[str, Any]]] = []

    if final_message:
        if payload := _parse_json_dict(final_message):
            candidates.append(("final_message", payload))

    wrapper_payload = _parse_json_dict(stdout_text)
    if wrapper_payload is not None:
        for key in _LLM_WRAPPER_PAYLOAD_KEYS:
            if key not in wrapper_payload:
                continue
            if payload := _parse_json_dict_from_value(wrapper_payload[key]):
                candidates.append((f"stdout_wrapper.{key}", payload))

    for source_text_name, source_text in (
        ("final_message", final_message or ""),
        ("stdout", stdout_text),
    ):
        if not source_text:
            continue
        candidates.extend(
            (f"{source_text_name}.fenced_json", payload)
            for payload in reversed(_iter_fenced_json_dicts(source_text))
        )

    for source_text_name, source_text in (
        ("final_message", final_message or ""),
        ("stdout", stdout_text),
    ):
        if not source_text:
            continue
        candidates.extend(
            (f"{source_text_name}.embedded_json", payload)
            for payload in reversed(_iter_embedded_json_dicts(source_text))
        )

    return _select_json_payload(
        candidates,
        response_schema=response_schema,
        response_format=response_format,
    )


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
    json_payload: dict[str, Any] | None = None,
    json_payload_source: str | None = None,
    json_payload_rejected_reason: str | None = None,
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
        json_payload=json_payload,
    )

    # Standardized Tracing Metadata
    run_summary = format_native_run_summary(result)
    reason_code, reason_detail = _detect_reason_code(
        status=status,
        timed_out=timed_out,
        exit_code=exit_code,
        summary=summary,
        stderr=stderr,
    )
    _record_span_data(
        output_data=run_summary,
        redactor=request.redactor,
    )
    # New canonical native telemetry attributes.
    set_current_span_attribute("code_agent.native.outcome_status", result.status)
    set_current_span_attribute("code_agent.native.reason_code", reason_code)
    set_current_span_attribute("code_agent.native.reason_detail", reason_detail)
    set_current_span_attribute(
        "code_agent.native.command", sanitize_command(command_text, request.redactor)
    )
    set_current_span_attribute("code_agent.native.timed_out", timed_out)
    set_current_span_attribute("code_agent.native.duration_seconds", elapsed)
    set_current_span_attribute(
        "code_agent.native.event_capture_enabled", request.events_path is not None
    )
    set_current_span_attribute("code_agent.native.artifact_root_present", bool(artifacts))
    if result.exit_code is not None:
        set_current_span_attribute("code_agent.native.exit_code", result.exit_code)
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
    if result.json_payload:
        # Record structured output for observability (parity with backup branch)
        set_current_span_attribute("llm.json_output", json.dumps(result.json_payload))
        if json_payload_source:
            set_current_span_attribute(f"{_JSON_PAYLOAD_ATTR_PREFIX}_source", json_payload_source)
        set_current_span_attribute(
            f"{_JSON_PAYLOAD_ATTR_PREFIX}_keys",
            ",".join(sorted(result.json_payload)),
        )
        add_current_span_event(
            "code_agent.native.json_payload_extracted",
            {
                "source": json_payload_source or "unknown",
                "keys_count": len(result.json_payload),
            },
        )
    elif json_payload_rejected_reason:
        set_current_span_attribute(
            f"{_JSON_PAYLOAD_ATTR_PREFIX}_rejected_reason",
            json_payload_rejected_reason,
        )
        add_current_span_event(
            "code_agent.native.json_payload_rejected",
            {"reason": json_payload_rejected_reason},
        )

    run_completed_payload: dict[str, str | int | float | bool] = {
        "status": result.status,
        "reason_code": reason_code,
        "timed_out": timed_out,
        "duration_seconds": elapsed,
        "has_json_payload": bool(result.json_payload),
        "has_final_message": bool(final_message),
        "files_changed_count": len(files_changed or []),
    }
    if result.exit_code is not None:
        run_completed_payload["exit_code"] = result.exit_code
    add_current_span_event("code_agent.native.run_completed", run_completed_payload)

    redacted_status_summary = redact_and_truncate_output(
        run_summary, redactor=request.redactor, limit_chars=NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH
    )
    set_span_status_from_outcome(result.status, redacted_status_summary)

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
    completed: subprocess.CompletedProcess[str] | None = None
    stdout_text: str = ""
    stderr_text: str = ""
    artifacts: list[ArtifactReference] = []
    with start_optional_span(
        tracer_name="workers.native_agent_runner",
        span_name="native_agent_run",
        attributes=with_span_kind(SPAN_KIND_AGENT),
        task_id=request.task_id,
        session_id=request.session_id,
    ):
        _record_span_data(
            input_data=request.prompt,
            redactor=request.redactor,
        )
        set_current_span_attribute(
            NATIVE_AGENT_COMMAND_ATTRIBUTE, sanitize_command(command_text, request.redactor)
        )

        try:
            max_retries = 2
            retry_count = 0

            while retry_count <= max_retries:
                try:
                    with with_llm_span(
                        tracer_name="workers.native_agent_runner",
                        span_name=f"native_agent_run.{request.span_kind.lower()}",
                        input_data=request.prompt,
                        task_id=request.task_id,
                        session_id=request.session_id,
                        kind=request.span_kind,
                    ):
                        # 1. Start with a minimal environment from the allowlist
                        effective_env: dict[str, str] = {
                            k: v
                            for k, v in os.environ.items()
                            if k.upper() in SAFE_SYSTEM_ENV_ALLOWLIST
                        }

                        # 2. Build a workspace-local HOME path we enforce later.
                        #    Keeping the value here avoids recomputing and ensures
                        #    the final force-overrides can always re-assert it.
                        agent_home = request.workspace_path / ".agent_home"
                        agent_home.mkdir(parents=True, exist_ok=True)
                        agent_home_value = str(agent_home)
                        effective_env["HOME"] = agent_home_value

                        # 3. Merge request.env after filtering against the sensitive denylist
                        if request.env:
                            for k, v in request.env.items():
                                k_upper = k.upper()
                                if k_upper == "HOME":
                                    logger.warning(
                                        "Native agent runner dropped protected environment key: %s",
                                        k,
                                    )
                                    continue
                                if k_upper in ALLOWED_AUTH_HOME_KEYS:
                                    effective_env[k] = v
                                    continue
                                is_denied = any(
                                    k_upper.startswith(prefix)
                                    for prefix in SENSITIVE_ENV_PREFIX_DENYLIST
                                )
                                if is_denied:
                                    logger.warning(
                                        "Native agent runner dropped sensitive environment key: %s",
                                        k,
                                    )
                                    continue
                                effective_env[k] = v

                        # 4. Apply force-set isolation overrides (MUST win over request.env)
                        effective_env.update(
                            {
                                "HOME": agent_home_value,
                                "CODEX_HOME": "/root/.codex",
                                "GEMINI_HOME": "/root/.gemini",
                                "CODE_AGENT_ENABLE_TRACING": "0",
                                "CODE_AGENT_ENABLE_TASK_SERVICE": "0",
                                "CODE_AGENT_INDEPENDENT_VERIFIER_ENABLED": "0",
                                "DATABASE_URL": f"sqlite:///{request.workspace_path}/.sandbox.db",
                                "TELEGRAM_BOT_TOKEN": "",
                            }
                        )

                        completed = subprocess.run(
                            request.command,
                            input=request.prompt,
                            check=False,
                            capture_output=True,
                            text=True,
                            cwd=repo_path,
                            env=inject_w3c_trace_context_env(effective_env),
                            timeout=request.timeout_seconds,
                        )
                        llm_output, llm_metadata = _split_llm_output_and_metadata(
                            completed.stdout or ""
                        )
                        set_llm_span_output(llm_output)
                        if llm_metadata is not None:
                            set_current_span_attribute(
                                f"{_LLM_METADATA_ATTR_PREFIX}.present",
                                True,
                            )
                            if isinstance(llm_metadata.get("session_id"), str):
                                set_current_span_attribute(
                                    f"{_LLM_METADATA_ATTR_PREFIX}.session_id",
                                    llm_metadata["session_id"],
                                )
                            set_current_span_attribute(
                                f"{_LLM_METADATA_ATTR_PREFIX}.json",
                                truncate_detail_keep_tail(
                                    json.dumps(llm_metadata, default=str),
                                    max_characters=NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH,
                                ),
                            )
                            add_current_span_event(
                                "code_agent.native.metadata_detected",
                                {
                                    "has_session_id": isinstance(
                                        llm_metadata.get("session_id"), str
                                    ),
                                    "metadata_keys_count": len(llm_metadata),
                                },
                            )

                    # Check for network-like errors in stdout/stderr even if it finished
                    output_text = (completed.stdout or "") + (completed.stderr or "")
                    if (
                        any(
                            err in output_text
                            for err in ["ECONNRESET", "socket hang up", "ETIMEDOUT"]
                        )
                        and retry_count < max_retries
                    ):
                        retry_count += 1
                        wait_seconds = 2**retry_count
                        logger.warning(
                            "Native agent encountered network error, retrying...",
                            extra={
                                "retry_count": retry_count,
                                "wait_seconds": wait_seconds,
                                "command": command_text,
                            },
                        )
                        add_current_span_event(
                            "code_agent.native.run_retry",
                            {
                                "retry_count": retry_count,
                                "wait_seconds": wait_seconds,
                                "retry_reason": "network_error",
                            },
                        )
                        time.sleep(wait_seconds)
                        continue

                    timed_out = False
                    break
                except subprocess.TimeoutExpired as exc:
                    stdout_text = _normalize_stream_payload(exc.stdout)
                    stderr_text = _normalize_stream_payload(exc.stderr)
                    output_text = stdout_text + stderr_text

                    if (
                        any(
                            err in output_text
                            for err in ["ECONNRESET", "socket hang up", "ETIMEDOUT"]
                        )
                        and retry_count < max_retries
                    ):
                        retry_count += 1
                        wait_seconds = 2**retry_count
                        logger.warning(
                            "Native agent timed out with network error, retrying...",
                            extra={
                                "retry_count": retry_count,
                                "wait_seconds": wait_seconds,
                                "command": command_text,
                            },
                        )
                        add_current_span_event(
                            "code_agent.native.run_retry",
                            {
                                "retry_count": retry_count,
                                "wait_seconds": wait_seconds,
                                "retry_reason": "network_timeout",
                            },
                        )
                        time.sleep(wait_seconds)
                        continue

                    try:
                        artifacts.extend(
                            _collect_standard_artifacts(
                                artifact_root=artifact_root,
                                stdout_text=stdout_text,
                                stderr_text=stderr_text,
                                events_path=events_path,
                            )
                        )
                    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as e:
                        logger.debug(
                            "Native agent runner failed to collect timeout artifacts: %s", e
                        )
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

        if completed is None:
            # Should be unreachable if the loop exited via break
            return _finalize_native_agent_run(
                request=request,
                status="error",
                summary="Native agent runner failed unexpectedly (process result missing).",
                command_text=command_text,
                started_at=started_at,
                timed_out=False,
            )

        timed_out = False
        stdout_text = completed.stdout or ""
        stderr_text = completed.stderr or ""
        try:
            artifacts.extend(
                _collect_standard_artifacts(
                    artifact_root=artifact_root,
                    stdout_text=stdout_text,
                    stderr_text=stderr_text,
                    events_path=events_path,
                )
            )

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

                # Detect systemic infrastructure failures (shell crashes, OOM, etc)
                # from non-zero exit codes. Use centralized truncation helper and
                # limit search space to tail to avoid performance issues with giant logs.
                stderr_tail = truncate_detail_keep_tail(stderr_text, max_characters=8192)
                if marker := find_infra_failure_marker(stderr_tail):
                    status = "error"
                    summary = f"SANDBOX_INFRA: detected shell crash ({marker})"
                elif "requires user confirmation" in stderr_tail.lower():
                    status = "error"
                    summary = (
                        "SANDBOX_INFRA: shell command blocked (requires user confirmation "
                        "in non-interactive mode)"
                    )
                elif "tool" in stderr_tail.lower() and "not found" in stderr_tail.lower():
                    status = "error"
                    truncated_stderr = truncate_detail_keep_tail(stderr_tail, max_characters=1024)
                    lines = truncated_stderr.strip().splitlines()
                    last_line = lines[-1] if lines else "unknown error"
                    summary = f"SANDBOX_INFRA: tool registry mismatch detected ({last_line})"
                elif completed.returncode in _SIGNAL_EXIT_CODES:
                    status = "error"
                    sig_name = _SIGNAL_EXIT_CODES[completed.returncode]
                    summary = f"SANDBOX_INFRA: detected shell crash ({sig_name})"

            json_payload, json_payload_source, json_payload_rejected_reason = (
                _extract_business_json_payload(
                    final_message=final_message,
                    stdout_text=stdout_text,
                    response_format=request.response_format,
                    response_schema=request.response_schema,
                )
            )

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
                json_payload=json_payload,
                json_payload_source=json_payload_source,
                json_payload_rejected_reason=json_payload_rejected_reason,
            )
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            logger.debug("Native agent runner artifact collection failed: %s", exc)
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
                artifacts=artifacts,
            )
