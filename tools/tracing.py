"""Shared tracing helpers with no-op fallback when OTEL is unavailable."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any


def start_optional_span(
    *,
    tracer_name: str,
    span_name: str,
    attributes: Mapping[str, Any] | None = None,
) -> Any:
    """Return an OTEL span context manager when tracing deps are available."""
    span_cm: Any = nullcontext()
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

        span_cm = otel_trace.get_tracer(tracer_name).start_as_current_span(
            span_name,
            attributes={
                key: value for key, value in (attributes or {}).items() if value is not None
            },
        )
    except ImportError:
        span_cm = nullcontext()
    return span_cm
