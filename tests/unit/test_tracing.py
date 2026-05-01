"""Unit tests for optional tracing helpers."""

from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

import pytest

import tools.tracing as tracing


@pytest.fixture(autouse=True)
def _reset_otel_trace_cache(monkeypatch) -> None:
    monkeypatch.setattr(tracing, "_CACHED_OTEL_TRACE", tracing._UNSET_OTEL_TRACE)
    monkeypatch.setattr(tracing, "_CACHED_OTEL_MODULE", tracing._UNSET_OTEL_TRACE)


def test_start_optional_span_yields_none_when_otel_is_unavailable(monkeypatch) -> None:
    """Import failures should fall back to yielding None."""
    monkeypatch.delitem(sys.modules, "opentelemetry", raising=False)
    real_import = builtins.__import__

    def _missing_otel(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "opentelemetry":
            raise ImportError("opentelemetry not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _missing_otel)

    with tracing.start_optional_span(
        tracer_name="tools.tracing",
        span_name="test.missing",
    ) as span:
        assert span is None


def test_start_optional_span_yields_span_and_filters_none_attributes(monkeypatch) -> None:
    """When OTEL is installed, the helper should yield the active span object."""
    recorded: list[dict[str, object]] = []

    class _FakeSpan:
        def __enter__(self):
            recorded.append({"entered": True})
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            recorded.append({"exited": True})
            return False

    class _FakeTracer:
        def start_as_current_span(
            self, name: str, attributes: dict[str, object] | None = None
        ) -> _FakeSpan:
            recorded.append({"name": name, "attributes": dict(attributes or {})})
            return _FakeSpan()

    class _FakeTraceApi:
        def get_tracer(self, name: str) -> _FakeTracer:
            recorded.append({"tracer_name": name})
            return _FakeTracer()

    monkeypatch.setitem(sys.modules, "opentelemetry", SimpleNamespace(trace=_FakeTraceApi()))

    with tracing.start_optional_span(
        tracer_name="tools.tracing",
        span_name="test.available",
        attributes={"kept": "value", "dropped": None},
    ) as span:
        assert span is not None

    assert recorded[0] == {"tracer_name": "tools.tracing"}
    assert recorded[1] == {
        "name": "test.available",
        "attributes": {"kept": "value"},
    }
    assert recorded[2] == {"entered": True}
    assert recorded[3] == {"exited": True}


def test_set_span_error_status_sets_error_status_when_available(monkeypatch) -> None:
    """Error-status helper should set OTEL status when APIs are available."""
    status_calls: list[object] = []

    class _FakeStatusCode:
        ERROR = "error"

    class _FakeStatus:
        def __init__(self, code: str, description: str | None = None) -> None:
            self.code = code
            self.description = description

    class _FakeSpan:
        def set_status(self, status: object) -> None:
            status_calls.append(status)

    class _FakeTraceApi:
        Status = _FakeStatus
        StatusCode = _FakeStatusCode

    monkeypatch.setitem(sys.modules, "opentelemetry", SimpleNamespace(trace=_FakeTraceApi()))

    tracing.set_span_error_status(_FakeSpan(), description="worker crashed unexpectedly")

    assert len(status_calls) == 1
    status = status_calls[0]
    assert isinstance(status, _FakeStatus)
    assert status.code == _FakeStatusCode.ERROR
    assert status.description == "worker crashed unexpectedly"


def test_start_optional_span_retries_after_cached_missing_when_module_appears(monkeypatch) -> None:
    """Cache should retry import when OTEL becomes available after a prior miss."""
    recorded: list[dict[str, object]] = []

    class _FakeSpan:
        def __enter__(self):
            recorded.append({"entered": True})
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

    class _FakeTracer:
        def start_as_current_span(
            self, name: str, attributes: dict[str, object] | None = None
        ) -> _FakeSpan:
            recorded.append({"name": name, "attributes": dict(attributes or {})})
            return _FakeSpan()

    class _FakeTraceApi:
        def get_tracer(self, name: str) -> _FakeTracer:
            recorded.append({"tracer_name": name})
            return _FakeTracer()

    monkeypatch.setattr(tracing, "_CACHED_OTEL_TRACE", None)
    monkeypatch.setattr(tracing, "_CACHED_OTEL_MODULE", None)
    monkeypatch.setitem(sys.modules, "opentelemetry", SimpleNamespace(trace=_FakeTraceApi()))

    with tracing.start_optional_span(
        tracer_name="tools.tracing",
        span_name="test.retry",
    ) as span:
        assert span is not None

    assert recorded[0] == {"tracer_name": "tools.tracing"}
