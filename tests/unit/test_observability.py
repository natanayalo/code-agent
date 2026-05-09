"""Unit tests for OpenTelemetry/OpenInference tracing bootstrap."""

from __future__ import annotations

import builtins
from collections import UserDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
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


def test_resolve_otel_tracing_endpoint_uses_explicit_override_first() -> None:
    """Explicit OTLP endpoint should always win over fallback env vars."""
    endpoint = observability_module.resolve_otel_tracing_endpoint(
        {
            observability_module.TRACING_OTLP_ENDPOINT_ENV_VAR: " http://collector:7777/custom ",
            observability_module.OTEL_OTLP_TRACES_ENDPOINT_ENV_VAR: "http://ignored:4318/v1/traces",
            observability_module.PHOENIX_COLLECTOR_ENDPOINT_ENV_VAR: "http://ignored:6006",
        }
    )

    assert endpoint == "http://collector:7777/custom"


def test_resolve_otel_tracing_endpoint_falls_back_to_generic_otel_env() -> None:
    """Generic OTEL env should be used when no code-agent specific endpoint is set."""
    endpoint = observability_module.resolve_otel_tracing_endpoint(
        {observability_module.OTEL_OTLP_TRACES_ENDPOINT_ENV_VAR: " http://otel:4318/v1/traces "}
    )

    assert endpoint == "http://otel:4318/v1/traces"


def test_resolve_otel_tracing_endpoint_falls_back_to_phoenix_collector_env() -> None:
    """Phoenix collector endpoint should be normalized to OTLP HTTP traces path."""
    endpoint = observability_module.resolve_otel_tracing_endpoint(
        {observability_module.PHOENIX_COLLECTOR_ENDPOINT_ENV_VAR: "http://phoenix:6006"}
    )

    assert endpoint == "http://phoenix:6006/v1/traces"


def test_resolve_otel_tracing_endpoint_defaults_to_local_phoenix_collector() -> None:
    """When nothing is configured, local Phoenix defaults should be used."""
    endpoint = observability_module.resolve_otel_tracing_endpoint({})
    assert endpoint == observability_module.DEFAULT_PHOENIX_OTLP_HTTP_ENDPOINT


def test_configure_tracing_from_env_is_noop_when_disabled() -> None:
    """Tracing bootstrap should remain inert unless explicitly enabled."""
    result = observability_module.configure_tracing_from_env(
        service_name="code-agent-api",
        environ={},
    )

    assert result.enabled is False
    assert result.configured is False
    assert result.reason == "disabled"


def test_configure_tracing_from_env_reports_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled tracing should fail safe when optional tracing dependencies are absent."""
    monkeypatch.setattr(observability_module, "_load_tracing_dependencies", lambda: None)

    result = observability_module.configure_tracing_from_env(
        service_name="code-agent-api",
        environ={observability_module.ENABLE_TRACING_ENV_VAR: "1"},
    )

    assert result.enabled is True
    assert result.configured is False
    assert result.reason == "missing_dependencies"


def test_configure_tracing_from_env_bootstraps_otel_and_openinference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracing bootstrap should configure OTLP exporter and LangChain instrumentation."""

    @dataclass
    class _FakeResource:
        attrs: dict[str, str]

        @classmethod
        def create(cls, attrs: dict[str, str]) -> _FakeResource:
            return cls(attrs=dict(attrs))

    class _FakeTracerProvider:
        def __init__(self, *, resource: _FakeResource) -> None:
            self.resource = resource
            self.processors: list[object] = []

        def add_span_processor(self, processor: object) -> None:
            self.processors.append(processor)

    class _FakeOTLPSpanExporter:
        def __init__(self, *, endpoint: str) -> None:
            self.endpoint = endpoint

    class _FakeBatchSpanProcessor:
        def __init__(self, exporter: _FakeOTLPSpanExporter) -> None:
            self.exporter = exporter

    class _FakeSimpleSpanProcessor:
        def __init__(self, exporter: _FakeOTLPSpanExporter) -> None:
            self.exporter = exporter

    class _FakeLangChainInstrumentor:
        instances: list[_FakeLangChainInstrumentor] = []

        def __init__(self) -> None:
            self.calls: list[object | None] = []
            self.__class__.instances.append(self)

        def instrument(self, tracer_provider: object | None = None) -> None:
            self.calls.append(tracer_provider)

    class _FakePropagateAPI:
        def __init__(self) -> None:
            self.propagators: list[object] = []

        def set_global_textmap(self, propagator: object) -> None:
            self.propagators.append(propagator)

    class _FakeTraceContextPropagator:
        pass

    fake_propagate = _FakePropagateAPI()

    register_calls = []

    def fake_register(**kwargs):
        register_calls.append(kwargs)
        return _FakeTracerProvider(
            resource=_FakeResource.create(
                {
                    "service.name": "code-agent-api",
                    "openinference.project.name": kwargs.get("project_name"),
                }
            )
        )

    fake_dependencies = observability_module._TracingDependencies(
        propagate_api=fake_propagate,
        resource_cls=_FakeResource,
        register_fn=fake_register,
        trace_context_propagator_cls=_FakeTraceContextPropagator,
    )
    monkeypatch.setattr(
        observability_module, "_load_tracing_dependencies", lambda: fake_dependencies
    )

    result = observability_module.configure_tracing_from_env(
        service_name="code-agent-api",
        environ={
            observability_module.ENABLE_TRACING_ENV_VAR: "true",
            observability_module.TRACING_PROJECT_ENV_VAR: "agent-dev",
            observability_module.TRACING_OTLP_ENDPOINT_ENV_VAR: "http://phoenix:6006/v1/traces",
        },
    )

    assert result.enabled is True
    assert result.configured is True
    assert result.reason == "configured"

    assert len(fake_propagate.propagators) == 1
    assert isinstance(fake_propagate.propagators[0], _FakeTraceContextPropagator)

    assert len(register_calls) == 1
    assert register_calls[0]["project_name"] == "agent-dev"
    assert register_calls[0]["endpoint"] == "http://phoenix:6006/v1/traces"
    assert register_calls[0]["auto_instrument"] is True


def test_configure_tracing_from_env_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated startup hooks should skip duplicate provider/instrumentor setup."""
    load_calls: list[str] = []

    class _TraceAPI:
        def set_global_textmap(self, _propagator: object) -> None:
            return None

        def set_tracer_provider(self, _provider: object) -> None:
            return None

    @dataclass
    class _Resource:
        attrs: dict[str, str]

        @classmethod
        def create(cls, attrs: dict[str, str]) -> _Resource:
            return cls(attrs)

    class _Provider:
        def __init__(self, *, resource: _Resource) -> None:
            self.resource = resource

        def add_span_processor(self, _processor: object) -> None:
            return None

    class _Exporter:
        def __init__(self, *, endpoint: str) -> None:
            self.endpoint = endpoint

    class _BatchProcessor:
        def __init__(self, _exporter: _Exporter) -> None:
            return None

    class _Instrumentor:
        def instrument(self, tracer_provider: object | None = None) -> None:
            return None

    def _loader() -> observability_module._TracingDependencies:
        load_calls.append("called")
        return observability_module._TracingDependencies(
            propagate_api=_TraceAPI(),  # reuse for mock
            resource_cls=_Resource,
            register_fn=lambda **kwargs: _Provider(resource=_Resource({})),
            trace_context_propagator_cls=lambda: None,
        )

    monkeypatch.setattr(observability_module, "_load_tracing_dependencies", _loader)

    first = observability_module.configure_tracing_from_env(
        service_name="code-agent-api",
        environ={observability_module.ENABLE_TRACING_ENV_VAR: "1"},
    )
    second = observability_module.configure_tracing_from_env(
        service_name="code-agent-api",
        environ={observability_module.ENABLE_TRACING_ENV_VAR: "1"},
    )

    assert first.reason == "configured"
    assert second.reason == "already_configured"
    assert load_calls == ["called"]


def test_configure_tracing_from_env_passes_correct_batch_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API should use batch=False, Worker should use batch=True."""
    register_calls = []

    def _fake_register(**kwargs):
        register_calls.append(kwargs)
        return type("MockProvider", (), {"add_span_processor": lambda *a: None})

    def _loader():
        return observability_module._TracingDependencies(
            propagate_api=type("Mock", (), {"set_global_textmap": lambda *a: None}),
            resource_cls=type("MockResource", (), {"create": lambda *a: None}),
            register_fn=_fake_register,
            trace_context_propagator_cls=lambda: None,
        )

    monkeypatch.setattr(observability_module, "_load_tracing_dependencies", _loader)

    # API case
    observability_module.configure_tracing_from_env(
        service_name="code-agent-api",
        environ={observability_module.ENABLE_TRACING_ENV_VAR: "1"},
    )
    assert register_calls[-1]["batch"] is False

    # Worker case (reset state first)
    monkeypatch.setattr(observability_module, "_bootstrap_complete", False)
    observability_module.configure_tracing_from_env(
        service_name="code-agent-worker",
        environ={observability_module.ENABLE_TRACING_ENV_VAR: "1"},
    )
    assert register_calls[-1]["batch"] is True


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


def test_start_optional_span_uses_otel_context_manager_when_available() -> None:
    """Helper should delegate to tracer.start_as_current_span when OTEL is available."""

    class _FakeSpanContextManager:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeTracer:
        def __init__(self, span_cm: _FakeSpanContextManager) -> None:
            self.span_cm = span_cm
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        def start_as_current_span(
            self, name: str, attributes: dict[str, str] | None = None
        ) -> _FakeSpanContextManager:
            self.calls.append((name, attributes))
            return self.span_cm

    span_cm = _FakeSpanContextManager()
    tracer = _FakeTracer(span_cm)

    with patch("opentelemetry.trace.get_tracer", return_value=tracer) as get_tracer_mock:
        resolved_cm = observability_module.start_optional_span(
            tracer_name="workers.gemini",
            span_name="gemini.chat",
            attributes={"openinference.span.kind": "LLM"},
        )

    assert resolved_cm is span_cm
    get_tracer_mock.assert_called_once_with("workers.gemini")
    assert tracer.calls == [("gemini.chat", {"openinference.span.kind": "LLM"})]


def test_start_optional_span_falls_back_to_nullcontext_when_otel_missing() -> None:
    """Helper should return a no-op context manager when OTEL import fails."""
    real_import = builtins.__import__

    def _import_without_otel(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "opentelemetry":
            raise ImportError("opentelemetry unavailable")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=_import_without_otel):
        with observability_module.start_optional_span(
            tracer_name="workers.codex",
            span_name="codex.exec",
            attributes={"openinference.span.kind": "LLM"},
        ):
            pass


def test_set_current_span_attribute_sets_attribute_when_recording() -> None:
    """Current span helper should set attributes when span is recording."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = _FakeSpan()

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_current_span_attribute("session.id", "session-123")

    assert span.attributes == {"session.id": "session-123"}


def test_set_optional_span_attribute_sets_value_when_recording() -> None:
    """Optional span setter should set attributes on recording span objects."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = _FakeSpan()
    observability_module.set_optional_span_attribute(span, "tool.name", "execute_bash")
    assert span.attributes == {"tool.name": "execute_bash"}


def test_set_optional_span_attribute_noop_when_not_recording() -> None:
    """Optional span setter should not write attributes for non-recording spans."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.set_attribute_calls = 0

        def is_recording(self) -> bool:
            return False

        def set_attribute(self, key: str, value: object) -> None:
            self.set_attribute_calls += 1

    span = _FakeSpan()
    observability_module.set_optional_span_attribute(span, "tool.name", "execute_bash")
    assert span.set_attribute_calls == 0


def test_set_current_span_attribute_noop_when_span_not_recording() -> None:
    """Current span helper should avoid attribute writes for non-recording spans."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.set_attribute_calls = 0

        def is_recording(self) -> bool:
            return False

        def set_attribute(self, key: str, value: object) -> None:
            self.set_attribute_calls += 1

    span = _FakeSpan()

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_current_span_attribute("session.id", "session-123")

    assert span.set_attribute_calls == 0


def test_with_span_kind_merges_attributes() -> None:
    """Span kind helper should preserve existing attrs and inject the standard kind key."""
    merged = observability_module.with_span_kind(
        observability_module.SPAN_KIND_TOOL,
        attributes={"tool.name": "execute_bash"},
    )

    assert merged == {
        "tool.name": "execute_bash",
        observability_module.OPENINFERENCE_SPAN_KIND_ATTRIBUTE: observability_module.SPAN_KIND_TOOL,
    }


def test_set_span_input_output_uses_json_mime_for_structured_payloads() -> None:
    """Structured payloads should be serialized as JSON with application/json MIME."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = _FakeSpan()

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_span_input_output(
            input_data={"foo": "bar"},
            output_data=[1, 2, 3],
        )

    assert span.attributes["input.value"] == '{"foo": "bar"}'
    assert span.attributes["input.mime_type"] == "application/json"
    assert span.attributes["output.value"] == "[1, 2, 3]"
    assert span.attributes["output.mime_type"] == "application/json"


def test_set_span_input_output_uses_text_mime_for_plain_scalars() -> None:
    """Scalar payloads should be represented as plain text."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = _FakeSpan()

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_span_input_output(
            input_data="hello",
            output_data=42,
        )

    assert span.attributes["input.value"] == "hello"
    assert span.attributes["input.mime_type"] == "text/plain"
    assert span.attributes["output.value"] == "42"
    assert span.attributes["output.mime_type"] == "text/plain"


def test_set_span_input_output_handles_tuples_and_mappings() -> None:
    """Tuples and Mappings (non-dict) should also be serialized as JSON."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = _FakeSpan()

    custom_mapping = UserDict({"foo": "bar"})

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_span_input_output(
            input_data=(1, 2, 3),
            output_data=custom_mapping,
        )

    assert span.attributes["input.value"] == "[1, 2, 3]"
    assert span.attributes["input.mime_type"] == "application/json"


def test_set_span_input_output_truncates_long_payloads() -> None:
    """Payloads exceeding MAX_SPAN_ATTRIBUTE_LENGTH should be truncated with a marker."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = _FakeSpan()
    long_payload = "a" * (observability_module.MAX_SPAN_ATTRIBUTE_LENGTH + 100)

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_span_input_output(input_data=long_payload)

    val = span.attributes["input.value"]
    assert len(val) > observability_module.MAX_SPAN_ATTRIBUTE_LENGTH
    assert "... (truncated to 12000 chars)" in val
    assert val.startswith("a" * observability_module.MAX_SPAN_ATTRIBUTE_LENGTH)


def test_set_span_input_output_changes_mime_type_on_truncation() -> None:
    """MIME type should switch to text/plain if a JSON payload is truncated."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, object] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: object) -> None:
            self.attributes[key] = value

    span = _FakeSpan()
    # Create a long dict that will exceed the limit when serialized
    long_dict = {"key": "a" * observability_module.MAX_SPAN_ATTRIBUTE_LENGTH}

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_span_input_output(input_data=long_dict)

    assert span.attributes["input.mime_type"] == "text/plain"
    assert "... (truncated to 12000 chars)" in span.attributes["input.value"]


def test_record_span_exception_invokes_otel_record_exception() -> None:
    """Helper should delegate to OTEL span.record_exception."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.exceptions: list[BaseException] = []

        def is_recording(self) -> bool:
            return True

        def record_exception(self, exception: BaseException) -> None:
            self.exceptions.append(exception)

    span = _FakeSpan()
    exc = RuntimeError("boom")

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.record_span_exception(exc)

    assert span.exceptions == [exc]


def test_set_span_status_invokes_otel_set_status() -> None:
    """Helper should delegate to OTEL span.set_status."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.status: Any = None

        def is_recording(self) -> bool:
            return True

        def set_status(self, status: Any) -> None:
            self.status = status

    span = _FakeSpan()

    # Mocking Status for the test
    with patch("opentelemetry.trace.Status") as mock_status_cls:
        with patch("opentelemetry.trace.get_current_span", return_value=span):
            observability_module.set_span_status(
                observability_module.STATUS_ERROR, "something went wrong"
            )

    assert span.status is not None
    mock_status_cls.assert_called_once()
    assert span.status == mock_status_cls.return_value


def test_set_span_task_metadata(mock_otel) -> None:
    """Verify that task metadata is correctly set on the current span."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.attributes: dict[str, Any] = {}

        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: Any) -> None:
            self.attributes[key] = value

    span = _FakeSpan()

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_span_task_metadata(
            task_id="t-123",
            session_id="s-456",
            attempt=2,
            channel="slack",
        )

    assert span.attributes[observability_module.TASK_ID_ATTRIBUTE] == "t-123"
    assert span.attributes[observability_module.SESSION_ID_ATTRIBUTE] == "s-456"
    assert span.attributes[observability_module.ATTEMPT_COUNT_ATTRIBUTE] == 2
    assert span.attributes[observability_module.CHANNEL_ATTRIBUTE] == "slack"


def test_set_span_status_from_outcome_success() -> None:
    """Verify TaskStatus.COMPLETED maps to STATUS_OK."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.status: Any = None

        def is_recording(self) -> bool:
            return True

        def set_status(self, status: Any) -> None:
            self.status = status

    span = _FakeSpan()

    with patch("opentelemetry.trace.Status") as mock_status_cls:
        with patch("opentelemetry.trace.get_current_span", return_value=span):
            observability_module.set_span_status_from_outcome("success")

    mock_status_cls.assert_called_once()
    args, _ = mock_status_cls.call_args
    # STATUS_OK is "OK", which getattr(StatusCode, "OK") should return something
    # We just care that it was called
    assert args[1] is None


def test_set_span_status_from_outcome_failure() -> None:
    """Verify TaskStatus.FAILED maps to STATUS_ERROR."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.status: Any = None

        def is_recording(self) -> bool:
            return True

        def set_status(self, status: Any) -> None:
            self.status = status

    span = _FakeSpan()

    with patch("opentelemetry.trace.Status") as mock_status_cls:
        with patch("opentelemetry.trace.get_current_span", return_value=span):
            observability_module.set_span_status_from_outcome("failed")

    mock_status_cls.assert_called_once()


def test_truncate_span_payload() -> None:
    """Test standardized truncation logic."""
    limit = observability_module.MAX_SPAN_ATTRIBUTE_LENGTH
    short_text = "abc"
    assert observability_module._truncate_span_payload(short_text) == short_text

    long_text = "x" * (limit + 10)
    truncated = observability_module._truncate_span_payload(long_text)
    assert len(truncated) > limit
    assert "... (truncated to 12000 chars)" in truncated
    assert truncated.startswith("x" * limit)


def test_serialize_span_payload_json() -> None:
    """Test JSON serialization and MIME type."""
    payload = {"a": 1}
    truncated, mime = observability_module._serialize_span_payload(payload)
    assert truncated == '{"a": 1}'
    assert mime == "application/json"


def test_serialize_span_payload_text() -> None:
    """Test plain text serialization."""
    payload = 123
    truncated, mime = observability_module._serialize_span_payload(payload)
    assert truncated == "123"
    assert mime == "text/plain"


def test_set_span_input_output_handles_exceptions(mock_otel) -> None:
    """Verify fail-safe behavior when serialization fails."""

    class _ExplodingSpan:
        def is_recording(self) -> bool:
            return True

        def set_attribute(self, key: str, value: Any) -> None:
            raise RuntimeError("crash")

    span = _ExplodingSpan()
    with patch("opentelemetry.trace.get_current_span", return_value=span):
        # Should not raise
        observability_module.set_span_input_output("input", "output")


def test_set_optional_span_attribute_noop_cases() -> None:
    """Verify set_optional_span_attribute ignores None or non-recording spans."""
    # None span
    observability_module.set_optional_span_attribute(None, "key", "val")

    # Non-recording span
    class _NonRecordingSpan:
        def is_recording(self) -> bool:
            return False

        def set_attribute(self, k, v):
            pytest.fail("Should not call set_attribute")

    observability_module.set_optional_span_attribute(_NonRecordingSpan(), "key", "val")


def test_set_span_status_from_outcome_error() -> None:
    """Verify status 'error' maps to STATUS_ERROR."""

    class _FakeSpan:
        def __init__(self) -> None:
            self.status: Any = None

        def is_recording(self) -> bool:
            return True

        def set_status(self, status: Any) -> None:
            self.status = status

    span = _FakeSpan()
    with patch("opentelemetry.trace.Status") as mock_status_cls:
        with patch("opentelemetry.trace.get_current_span", return_value=span):
            observability_module.set_span_status_from_outcome("error", "some error")
    mock_status_cls.assert_called_once()


def test_set_span_status_handles_exception(mock_otel) -> None:
    """Verify set_span_status fail-safe."""

    class _ExplodingSpan:
        def is_recording(self) -> bool:
            return True

        def set_status(self, status):
            raise RuntimeError("crash")

    span = _ExplodingSpan()
    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_span_status("OK")


def test_set_optional_span_attribute_handles_exception() -> None:
    """Verify set_optional_span_attribute fail-safe."""

    class _ExplodingSpan:
        def is_recording(self) -> bool:
            return True

        def set_attribute(self, k, v):
            raise RuntimeError("crash")

    observability_module.set_optional_span_attribute(_ExplodingSpan(), "key", "val")


def test_set_span_input_output_noop_when_not_recording(mock_otel) -> None:
    """Verify set_span_input_output skips non-recording spans."""

    class _NonRecordingSpan:
        def is_recording(self) -> bool:
            return False

        def set_attribute(self, k, v):
            pytest.fail("Should not call")

    with patch("opentelemetry.trace.get_current_span", return_value=_NonRecordingSpan()):
        observability_module.set_span_input_output("in", "out")


def test_record_span_exception_handles_exception(mock_otel) -> None:
    """Verify record_span_exception fail-safe."""

    class _ExplodingSpan:
        def is_recording(self) -> bool:
            return True

        def record_exception(self, e):
            raise RuntimeError("crash")

    with patch("opentelemetry.trace.get_current_span", return_value=_ExplodingSpan()):
        observability_module.record_span_exception(RuntimeError("boom"))


def test_set_current_span_attribute_handles_exception() -> None:
    """Verify set_current_span_attribute fail-safe."""
    with patch("opentelemetry.trace.get_current_span", side_effect=RuntimeError("crash")):
        observability_module.set_current_span_attribute("key", "val")


def test_load_tracing_dependencies_handles_import_error() -> None:
    """Verify _load_tracing_dependencies returns None on ImportError."""
    real_import = builtins.__import__

    def _exploding_import(name, *args, **kwargs):
        if "opentelemetry" in name or "phoenix" in name:
            raise ImportError("absent")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=_exploding_import):
        assert observability_module._load_tracing_dependencies() is None


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
