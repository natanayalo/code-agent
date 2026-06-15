from __future__ import annotations

import json
import logging
import time
from typing import Any, Final, Literal

from apps.observability import (
    NATIVE_AGENT_DURATION_ATTRIBUTE,
    NATIVE_AGENT_EXIT_CODE_ATTRIBUTE,
    NATIVE_AGENT_STDERR_ATTRIBUTE,
    NATIVE_AGENT_STDOUT_ATTRIBUTE,
    NATIVE_AGENT_TIMED_OUT_ATTRIBUTE,
    NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH,
)
from sandbox.redact import redact_and_truncate_output, sanitize_command
from workers.adapter_utils import format_native_run_summary
from workers.base import ArtifactReference
from workers.native_agent_messages import _detect_reason_code

# We need to import NativeAgentRunRequest and _record_span_data
# from native_agent_runner since they are defined there
from workers.native_agent_models import NativeAgentRunRequest, NativeAgentRunResult
from workers.native_agent_tracing import _record_span_data

_JSON_PAYLOAD_ATTR_PREFIX: Final[str] = "code_agent.native_agent.json_payload"
from apps.observability import (  # noqa: E402
    add_current_span_event,
    set_current_span_attribute,
    set_span_status_from_outcome,
)

logger = logging.getLogger(__name__)


def _record_native_agent_telemetry(
    request: NativeAgentRunRequest,
    result: NativeAgentRunResult,
    run_summary: str,
    reason_code: str,
    reason_detail: str | None,
    json_payload_source: str | None,
    json_payload_rejected_reason: str | None,
) -> None:
    _record_span_data(
        output_data=run_summary,
        redactor=request.redactor,
    )
    # New canonical native telemetry attributes.
    set_current_span_attribute("code_agent.native_agent.outcome_status", result.status)
    set_current_span_attribute("code_agent.native_agent.reason_code", reason_code)
    set_current_span_attribute("code_agent.native_agent.reason_detail", reason_detail)
    set_current_span_attribute(
        "code_agent.native_agent.command", sanitize_command(result.command, request.redactor)
    )
    set_current_span_attribute("code_agent.native_agent.timed_out", result.timed_out)
    set_current_span_attribute("code_agent.native_agent.duration_seconds", result.duration_seconds)
    set_current_span_attribute(
        "code_agent.native_agent.event_capture_enabled", request.events_path is not None
    )
    set_current_span_attribute(
        "code_agent.native_agent.artifact_root_present", bool(result.artifacts)
    )
    if result.exit_code is not None:
        set_current_span_attribute("code_agent.native_agent.exit_code", result.exit_code)
        set_current_span_attribute(NATIVE_AGENT_EXIT_CODE_ATTRIBUTE, result.exit_code)
    set_current_span_attribute(NATIVE_AGENT_TIMED_OUT_ATTRIBUTE, result.timed_out)
    set_current_span_attribute(NATIVE_AGENT_DURATION_ATTRIBUTE, result.duration_seconds)
    set_current_span_attribute(
        NATIVE_AGENT_STDOUT_ATTRIBUTE,
        redact_and_truncate_output(
            result.stdout or "",
            redactor=request.redactor,
            limit_chars=NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH,
        ),
    )
    set_current_span_attribute(
        NATIVE_AGENT_STDERR_ATTRIBUTE,
        redact_and_truncate_output(
            result.stderr or "",
            redactor=request.redactor,
            limit_chars=NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH,
        ),
    )
    _record_native_agent_json_payload_telemetry(
        result=result,
        json_payload_source=json_payload_source,
        json_payload_rejected_reason=json_payload_rejected_reason,
    )
    _record_native_agent_run_completed_telemetry(
        result=result,
        reason_code=reason_code,
    )

    redacted_status_summary = redact_and_truncate_output(
        run_summary, redactor=request.redactor, limit_chars=NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH
    )
    set_span_status_from_outcome(result.status, redacted_status_summary)


def _record_native_agent_json_payload_telemetry(
    result: NativeAgentRunResult,
    json_payload_source: str | None,
    json_payload_rejected_reason: str | None,
) -> None:
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
            "code_agent.native_agent.json_payload_extracted",
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
            "code_agent.native_agent.json_payload_rejected",
            {"reason": json_payload_rejected_reason},
        )


def _record_native_agent_run_completed_telemetry(
    result: NativeAgentRunResult,
    reason_code: str,
) -> None:
    run_completed_payload: dict[str, str | int | float | bool] = {
        "status": result.status,
        "reason_code": reason_code,
        "timed_out": result.timed_out,
        "duration_seconds": result.duration_seconds,
        "has_json_payload": bool(result.json_payload),
        "has_final_message": bool(result.final_message),
        "files_changed_count": len(result.files_changed),
    }
    if result.exit_code is not None:
        run_completed_payload["exit_code"] = result.exit_code
    add_current_span_event("code_agent.native_agent.run_completed", run_completed_payload)


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
    friction_reports: list[dict[str, Any]] | None = None,
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
        friction_reports=friction_reports or [],
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
    _record_native_agent_telemetry(
        request=request,
        result=result,
        run_summary=run_summary,
        reason_code=reason_code,
        reason_detail=reason_detail,
        json_payload_source=json_payload_source,
        json_payload_rejected_reason=json_payload_rejected_reason,
    )

    return result
