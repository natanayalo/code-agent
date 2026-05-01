"""Unit tests for OpenTelemetry/OpenInference tracing bootstrap."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from apps import observability as observability_module


@pytest.fixture(autouse=True)
def _reset_bootstrap_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observability_module, "_bootstrap_complete", False)


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

    class _FakeTraceAPI:
        def __init__(self) -> None:
            self.providers: list[object] = []

        def set_tracer_provider(self, provider: object) -> None:
            self.providers.append(provider)

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

    fake_trace = _FakeTraceAPI()
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
        trace_api=fake_trace,
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
            trace_api=_TraceAPI(),
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
            trace_api=type("Mock", (), {"set_global_textmap": lambda *a: None}),
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
