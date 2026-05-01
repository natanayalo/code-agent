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

    class _FakeLangChainInstrumentor:
        instances: list[_FakeLangChainInstrumentor] = []

        def __init__(self) -> None:
            self.calls: list[object | None] = []
            self.__class__.instances.append(self)

        def instrument(self, tracer_provider: object | None = None) -> None:
            self.calls.append(tracer_provider)

    fake_trace = _FakeTraceAPI()
    fake_dependencies = observability_module._TracingDependencies(
        trace_api=fake_trace,
        resource_cls=_FakeResource,
        tracer_provider_cls=_FakeTracerProvider,
        batch_span_processor_cls=_FakeBatchSpanProcessor,
        otlp_exporter_cls=_FakeOTLPSpanExporter,
        langchain_instrumentor_cls=_FakeLangChainInstrumentor,
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
    assert fake_trace.providers

    provider = fake_trace.providers[0]
    assert isinstance(provider, _FakeTracerProvider)
    assert provider.resource.attrs["service.name"] == "code-agent-api"
    assert provider.resource.attrs["openinference.project.name"] == "agent-dev"
    assert provider.processors
    processor = provider.processors[0]
    assert isinstance(processor, _FakeBatchSpanProcessor)
    assert processor.exporter.endpoint == "http://phoenix:6006/v1/traces"
    assert _FakeLangChainInstrumentor.instances[0].calls == [provider]


def test_configure_tracing_from_env_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated startup hooks should skip duplicate provider/instrumentor setup."""
    load_calls: list[str] = []

    class _TraceAPI:
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
            resource_cls=_Resource,
            tracer_provider_cls=_Provider,
            batch_span_processor_cls=_BatchProcessor,
            otlp_exporter_cls=_Exporter,
            langchain_instrumentor_cls=_Instrumentor,
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


def test_configure_tracing_from_env_supports_instrumentor_without_provider_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap should fall back when instrumentor does not accept tracer_provider kwarg."""

    class _TraceAPI:
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

    class _InstrumentorWithoutKwarg:
        calls = 0

        def instrument(self) -> None:
            self.__class__.calls += 1

    monkeypatch.setattr(
        observability_module,
        "_load_tracing_dependencies",
        lambda: observability_module._TracingDependencies(
            trace_api=_TraceAPI(),
            resource_cls=_Resource,
            tracer_provider_cls=_Provider,
            batch_span_processor_cls=_BatchProcessor,
            otlp_exporter_cls=_Exporter,
            langchain_instrumentor_cls=_InstrumentorWithoutKwarg,
        ),
    )

    result = observability_module.configure_tracing_from_env(
        service_name="code-agent-api",
        environ={observability_module.ENABLE_TRACING_ENV_VAR: "true"},
    )

    assert result.configured is True
    assert _InstrumentorWithoutKwarg.calls == 1
