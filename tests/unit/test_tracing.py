"""Unit tests for optional tracing helpers."""

from __future__ import annotations

import builtins
import sys
from types import SimpleNamespace

from tools.tracing import start_optional_span


def test_start_optional_span_yields_none_when_otel_is_unavailable(monkeypatch) -> None:
    """Import failures should fall back to yielding None."""
    monkeypatch.delitem(sys.modules, "opentelemetry", raising=False)
    real_import = builtins.__import__

    def _missing_otel(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "opentelemetry":
            raise ImportError("opentelemetry not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _missing_otel)

    with start_optional_span(
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

    with start_optional_span(
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
