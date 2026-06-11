"""Stateless utility functions for tracing standardisation and payload formatting."""

import json
import logging
from collections.abc import Mapping
from typing import Any

from apps.observability import (
    ATTEMPT_COUNT_ATTRIBUTE,
    ATTR_ROUTE_REASON,
    ATTR_TASK_KIND,
    ATTR_VERIFICATION_SUMMARY,
    CHANNEL_ATTRIBUTE,
    MAX_SPAN_ATTRIBUTE_LENGTH,
    SESSION_ID_ATTRIBUTE,
    TASK_ID_ATTRIBUTE,
)

logger = logging.getLogger(__name__)


def get_centralized_span_input_data(
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    attempt: int | None = None,
    channel: str | None = None,
    task_kind: str | None = None,
    route_reason: str | None = None,
    verification_summary: str | None = None,
    extra_attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consolidate standard task correlation attributes into a span attribute dictionary."""
    attributes: dict[str, Any] = dict(extra_attributes) if extra_attributes is not None else {}
    if task_id:
        attributes[TASK_ID_ATTRIBUTE] = task_id
    if session_id:
        attributes[SESSION_ID_ATTRIBUTE] = session_id
    if attempt is not None:
        attributes[ATTEMPT_COUNT_ATTRIBUTE] = attempt
    if channel:
        attributes[CHANNEL_ATTRIBUTE] = channel
    if task_kind:
        attributes[ATTR_TASK_KIND] = task_kind
    if route_reason:
        attributes[ATTR_ROUTE_REASON] = route_reason
    if verification_summary:
        attributes[ATTR_VERIFICATION_SUMMARY] = verification_summary
    return attributes


def get_centralized_result_mapping() -> dict[str, Any] | None:
    """Return the centralized mapping of string statuses to OpenTelemetry StatusCode enum values."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

        return {
            "success": otel_trace.StatusCode.OK,
            "completed": otel_trace.StatusCode.OK,
            "ok": otel_trace.StatusCode.OK,
            "blocked_on_clarification": otel_trace.StatusCode.UNSET,
            "error": otel_trace.StatusCode.ERROR,
            "failure": otel_trace.StatusCode.ERROR,
            "failed": otel_trace.StatusCode.ERROR,
            "cancelled": otel_trace.StatusCode.ERROR,
            "unset": otel_trace.StatusCode.UNSET,
        }
    except ImportError:
        return None


def resolve_span_status_code(status: str) -> Any:
    """Map a string status to an OpenTelemetry StatusCode enum value."""
    try:
        mapping = get_centralized_result_mapping()
        if mapping is None:
            return None
        return mapping.get(status.lower(), mapping["unset"])
    except Exception as exc:
        logger.debug("Failed to resolve span status code: %s", exc)
        return None


def get_centralized_span_status(
    status: str,
    description: str | None = None,
) -> Any:
    """Map a standard outcome status (success/error/failure) to an OpenTelemetry Status object."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

        status_code = resolve_span_status_code(status)
        if status_code is not None:
            return otel_trace.Status(status_code, description)

        return None
    except Exception as exc:
        logger.debug("Failed to map span status: %s", exc)
        return None


def truncate_span_payload(value: str) -> str:
    """Standardized truncation for span attributes to prevent oversized payloads."""
    if len(value) <= MAX_SPAN_ATTRIBUTE_LENGTH:
        return value
    truncated = value[:MAX_SPAN_ATTRIBUTE_LENGTH]
    return f"{truncated}\n... (truncated to {MAX_SPAN_ATTRIBUTE_LENGTH} chars)"


def serialize_span_payload(payload: Any) -> tuple[str, str]:
    """Serialize payloads for OpenInference input/output span attributes."""
    if isinstance(payload, Mapping | list | tuple):
        actual_payload = dict(payload) if isinstance(payload, Mapping) else payload
        serialized = json.dumps(actual_payload, default=str)
        mime_type = "application/json"
    else:
        serialized = str(payload)
        mime_type = "text/plain"

    truncated = truncate_span_payload(serialized)
    if len(serialized) > MAX_SPAN_ATTRIBUTE_LENGTH:
        mime_type = "text/plain"

    return truncated, mime_type
