"""Unit tests for OpenTelemetry/OpenInference tracing bootstrap."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from apps import observability as observability_module


@pytest.fixture(autouse=True)
def _reset_bootstrap_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability_module, "_bootstrap_complete", False)


@pytest.fixture
def mock_otel(monkeypatch):
    class _FakePropagateAPI:
        def __init__(self):
            self.inject = MagicMock()
            self.extract = MagicMock()
            self.set_global_textmap = MagicMock()

    fake_propagate = _FakePropagateAPI()

    fake_deps = observability_module._TracingDependencies(
        propagate_api=fake_propagate,
        resource_cls=MagicMock(),
        register_fn=MagicMock(),
        trace_context_propagator_cls=MagicMock(),
    )

    monkeypatch.setattr(observability_module, "_load_tracing_dependencies", lambda: fake_deps)
    return fake_deps


def test_capture_restore_trace_context(mock_otel):
    """Verify that trace context can be captured into a dict and restored."""
    deps = observability_module._load_tracing_dependencies()

    # Mock inject to set a dummy traceparent
    def mock_inject(carrier, context=None):
        carrier["traceparent"] = "00-test-trace-id-test-span-id-01"

    deps.propagate_api.inject.side_effect = mock_inject
    deps.propagate_api.extract.return_value = "mock-token"

    # 1. Capture
    context = observability_module.capture_trace_context()
    assert context == {"traceparent": "00-test-trace-id-test-span-id-01"}
    deps.propagate_api.inject.assert_called_once()

    # 2. Restore
    with patch("opentelemetry.context.attach") as mock_attach:
        token = observability_module.restore_trace_context(context)
        deps.propagate_api.extract.assert_called_once_with(carrier=context)
        mock_attach.assert_called_once_with("mock-token")
        assert token == mock_attach.return_value


def test_inject_w3c_trace_context_env_uses_captured_context() -> None:
    """Helper should merge captured trace headers into environment variables."""
    base_env = {"PYTHONUNBUFFERED": "1"}
    trace_context = {
        "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
        "tracestate": "vendor=value",
        "baggage": "session.id=abc",
    }
    with patch.object(
        observability_module,
        "capture_trace_context",
        return_value=trace_context,
    ):
        merged = observability_module.inject_w3c_trace_context_env(base_env)

    assert merged == {
        "PYTHONUNBUFFERED": "1",
        "TRACEPARENT": "00-11111111111111111111111111111111-2222222222222222-01",
        "TRACESTATE": "vendor=value",
        "BAGGAGE": "session.id=abc",
    }


def test_inject_w3c_trace_context_env_preserves_existing_values() -> None:
    """Existing explicit env values should not be overridden by captured context."""
    merged = observability_module.inject_w3c_trace_context_env(
        {
            "TRACEPARENT": "existing-traceparent",
            "TRACESTATE": "existing-tracestate",
            "BAGGAGE": "existing-baggage",
        },
        trace_context={
            "traceparent": "new-traceparent",
            "tracestate": "new-tracestate",
            "baggage": "new-baggage",
        },
    )

    assert merged == {
        "TRACEPARENT": "existing-traceparent",
        "TRACESTATE": "existing-tracestate",
        "BAGGAGE": "existing-baggage",
    }


def test_restore_trace_context_noop():
    """Verify that restore_trace_context handles empty input gracefully."""
    assert observability_module.restore_trace_context(None) is None
    assert observability_module.restore_trace_context({}) is None


def test_detach_trace_context_noop_when_token_missing() -> None:
    """Detaching a missing token should be a no-op."""
    observability_module.detach_trace_context(None)


def test_detach_trace_context_invokes_otel_detach() -> None:
    """Detach helper should delegate to OpenTelemetry context API when available."""
    with patch("opentelemetry.context.detach") as detach_mock:
        observability_module.detach_trace_context("mock-token")
    detach_mock.assert_called_once_with("mock-token")


def test_with_restored_trace_context_restores_and_detaches() -> None:
    """Context manager should always pair restore + detach calls."""
    context = {"traceparent": "00-abc-def-01"}
    with patch.object(
        observability_module,
        "restore_trace_context",
        return_value="mock-token",
    ) as restore_mock:
        with patch.object(observability_module, "detach_trace_context") as detach_mock:
            with observability_module.with_restored_trace_context(context):
                pass

    restore_mock.assert_called_once_with(context)
    detach_mock.assert_called_once_with("mock-token")


def test_with_restored_trace_context_detaches_on_error() -> None:
    """Context manager should detach context even when wrapped code fails."""
    context = {"traceparent": "00-abc-def-01"}
    with patch.object(
        observability_module,
        "restore_trace_context",
        return_value="mock-token",
    ):
        with patch.object(observability_module, "detach_trace_context") as detach_mock:
            with pytest.raises(RuntimeError, match="boom"):
                with observability_module.with_restored_trace_context(context):
                    raise RuntimeError("boom")

    detach_mock.assert_called_once_with("mock-token")


def test_bind_current_trace_context_restores_scope_and_preserves_func_attr() -> None:
    """Bound callables should run inside captured trace scope and keep func metadata."""

    captured_scopes: list[dict[str, str] | None] = []

    @contextmanager
    def _fake_scope(context: dict[str, str] | None):
        captured_scopes.append(context)
        yield

    def _target() -> str:
        return "ok"

    trace_context = {"traceparent": "00-abc-def-01"}
    with patch.object(
        observability_module,
        "capture_trace_context",
        return_value=trace_context,
    ):
        with patch.object(observability_module, "with_restored_trace_context", _fake_scope):
            wrapped = observability_module.bind_current_trace_context(_target)
            assert wrapped() == "ok"

    assert captured_scopes == [trace_context]
    assert wrapped.func is _target  # type: ignore[attr-defined]


def test_bind_current_trace_context_respects_original_func_override() -> None:
    """Optional original_func should drive func metadata used by thread-runner tests."""

    @contextmanager
    def _fake_scope(_context: dict[str, str] | None):
        yield

    def _target() -> str:
        return "ok"

    def _original() -> str:
        return "original"

    with patch.object(observability_module, "capture_trace_context", return_value={}):
        with patch.object(observability_module, "with_restored_trace_context", _fake_scope):
            wrapped = observability_module.bind_current_trace_context(
                _target,
                original_func=_original,
            )

    assert wrapped.func is _original  # type: ignore[attr-defined]


def test_capture_trace_context_without_deps() -> None:
    """Verify capture_trace_context returns empty dict when deps missing."""
    with patch.object(observability_module, "_load_tracing_dependencies", return_value=None):
        assert observability_module.capture_trace_context() == {}


def test_restore_trace_context_without_deps() -> None:
    """Verify restore_trace_context returns None when deps missing."""
    with patch.object(observability_module, "_load_tracing_dependencies", return_value=None):
        assert observability_module.restore_trace_context({"traceparent": "00-1-2-01"}) is None


def test_detach_trace_context_handles_errors() -> None:
    """Verify detach_trace_context fail-safe for RuntimeError."""
    with patch("opentelemetry.context.detach", side_effect=RuntimeError("detached already")):
        observability_module.detach_trace_context("mock-token")

    with patch("opentelemetry.context.detach", side_effect=AttributeError("no detach")):
        observability_module.detach_trace_context("mock-token")
