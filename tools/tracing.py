"""Shared tracing helpers with no-op fallback when OTEL is unavailable."""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

OPENINFERENCE_SPAN_KIND = "openinference.span.kind"
OPENINFERENCE_SPAN_KIND_AGENT = "AGENT"
OPENINFERENCE_SPAN_KIND_CHAIN = "CHAIN"
OPENINFERENCE_SPAN_KIND_TOOL = "TOOL"

_UNSET_OTEL_TRACE = object()
_CACHED_OTEL_TRACE: Any | object = _UNSET_OTEL_TRACE
_CACHED_OTEL_MODULE: Any | object = _UNSET_OTEL_TRACE


def _get_otel_trace() -> Any | None:
    """Return cached OTEL trace API if installed, else None."""
    global _CACHED_OTEL_MODULE, _CACHED_OTEL_TRACE
    current_module = sys.modules.get("opentelemetry")
    if (
        _CACHED_OTEL_TRACE is _UNSET_OTEL_TRACE
        or _CACHED_OTEL_MODULE is _UNSET_OTEL_TRACE
        or _CACHED_OTEL_MODULE is not current_module
    ):
        try:
            from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]
        except ImportError:
            _CACHED_OTEL_TRACE = None
        else:
            _CACHED_OTEL_TRACE = otel_trace
        _CACHED_OTEL_MODULE = sys.modules.get("opentelemetry")
    return None if _CACHED_OTEL_TRACE is _UNSET_OTEL_TRACE else _CACHED_OTEL_TRACE


@contextmanager
def start_optional_span(
    *,
    tracer_name: str,
    span_name: str,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any | None]:
    """Yield an OTEL span when tracing deps are available, else yield None."""
    otel_trace = _get_otel_trace()
    if otel_trace is None:
        yield None
        return

    filtered_attributes = {
        key: value for key, value in (attributes or {}).items() if value is not None
    }
    with otel_trace.get_tracer(tracer_name).start_as_current_span(
        span_name,
        attributes=filtered_attributes,
    ) as span:
        yield span


def set_span_error_status(span: Any, *, description: str | None = None) -> None:
    """Mark a span as errored when OTEL status APIs are available."""
    if span is None:
        return

    otel_trace = _get_otel_trace()
    if otel_trace is None:
        return

    set_status = getattr(span, "set_status", None)
    status_cls = getattr(otel_trace, "Status", None)
    status_code = getattr(otel_trace, "StatusCode", None)
    if not callable(set_status) or status_cls is None or status_code is None:
        return

    error_code = getattr(status_code, "ERROR", None)
    if error_code is None:
        return

    if description is None:
        set_status(status_cls(error_code))
    else:
        set_status(status_cls(error_code, description=description))


def get_current_traceparent() -> str | None:
    """Return the current OTEL traceparent string for manual propagation."""
    otel_trace = _get_otel_trace()
    if otel_trace is None:
        return None
    try:
        from opentelemetry import propagate  # type: ignore[import-not-found]

        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        return carrier.get("traceparent")
    except Exception:
        return None


@contextmanager
def use_traceparent(traceparent: str | None) -> Iterator[None]:
    """Inject a traceparent into the current OTEL context for the duration of the block."""
    otel_trace = _get_otel_trace()
    if otel_trace is None or not traceparent:
        yield
        return
    try:
        from opentelemetry import context  # type: ignore[import-not-found]
        from opentelemetry.trace.propagation.tracecontext import (  # type: ignore[import-not-found]
            TraceContextTextMapPropagator,
        )

        # Ensure we strip quotes if the string came from a JSON-serialized source
        clean_traceparent = traceparent.strip('"') if traceparent else None
        ctx = TraceContextTextMapPropagator().extract({"traceparent": clean_traceparent})
        token = context.attach(ctx)
        try:
            yield
        finally:
            context.detach(token)
    except Exception:
        yield
