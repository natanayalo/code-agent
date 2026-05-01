"""Shared tracing helpers with no-op fallback when OTEL is unavailable."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any


@contextmanager
def start_optional_span(
    *,
    tracer_name: str,
    span_name: str,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any | None]:
    """Yield an OTEL span when tracing deps are available, else yield None."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

        filtered_attributes = {
            key: value for key, value in (attributes or {}).items() if value is not None
        }
        with otel_trace.get_tracer(tracer_name).start_as_current_span(
            span_name,
            attributes=filtered_attributes,
        ) as span:
            yield span
    except ImportError:
        yield None
