"""OpenTelemetry/OpenInference tracing bootstrap for app runtimes."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any, Final

logger = logging.getLogger(__name__)

ENABLE_TRACING_ENV_VAR: Final[str] = "CODE_AGENT_ENABLE_TRACING"
TRACING_PROJECT_ENV_VAR: Final[str] = "CODE_AGENT_TRACING_PROJECT"
TRACING_OTLP_ENDPOINT_ENV_VAR: Final[str] = "CODE_AGENT_TRACING_OTLP_ENDPOINT"
OTEL_OTLP_TRACES_ENDPOINT_ENV_VAR: Final[str] = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
PHOENIX_COLLECTOR_ENDPOINT_ENV_VAR: Final[str] = "PHOENIX_COLLECTOR_ENDPOINT"
DEFAULT_TRACING_PROJECT: Final[str] = "code-agent"
DEFAULT_PHOENIX_OTLP_HTTP_ENDPOINT: Final[str] = "http://127.0.0.1:6006/v1/traces"

_bootstrap_lock = Lock()
_bootstrap_complete = False


@dataclass(frozen=True)
class TracingBootstrapResult:
    """Structured result describing a tracing bootstrap attempt."""

    enabled: bool
    configured: bool
    reason: str
    project_name: str | None = None
    otlp_endpoint: str | None = None


@dataclass(frozen=True)
class _TracingDependencies:
    trace_api: Any
    propagate_api: Any
    resource_cls: Any
    register_fn: Any
    trace_context_propagator_cls: Any


def _is_enabled(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _to_phoenix_otlp_http_endpoint(collector_endpoint: str) -> str:
    normalized = collector_endpoint.rstrip("/")
    if normalized.endswith("/v1/traces"):
        return normalized
    return f"{normalized}/v1/traces"


def resolve_otel_tracing_endpoint(environ: Mapping[str, str]) -> str:
    """Resolve OTLP traces endpoint using explicit and Phoenix-style env vars."""
    explicit_otlp_endpoint = _clean(environ.get(TRACING_OTLP_ENDPOINT_ENV_VAR))
    if explicit_otlp_endpoint is not None:
        return explicit_otlp_endpoint

    generic_otel_endpoint = _clean(environ.get(OTEL_OTLP_TRACES_ENDPOINT_ENV_VAR))
    if generic_otel_endpoint is not None:
        return generic_otel_endpoint

    phoenix_collector_endpoint = _clean(environ.get(PHOENIX_COLLECTOR_ENDPOINT_ENV_VAR))
    if phoenix_collector_endpoint is not None:
        return _to_phoenix_otlp_http_endpoint(phoenix_collector_endpoint)

    return DEFAULT_PHOENIX_OTLP_HTTP_ENDPOINT


def resolve_tracing_project_name(environ: Mapping[str, str]) -> str:
    """Resolve the logical tracing project name."""
    return _clean(environ.get(TRACING_PROJECT_ENV_VAR)) or DEFAULT_TRACING_PROJECT


def _load_tracing_dependencies() -> _TracingDependencies | None:
    try:
        from opentelemetry import propagate as propagate_api  # type: ignore[import-not-found]
        from opentelemetry import trace as trace_api  # type: ignore[import-not-found]
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.trace.propagation.tracecontext import (  # type: ignore[import-not-found]
            TraceContextTextMapPropagator,
        )
        from phoenix.otel import register as register_fn  # type: ignore[import-not-found]
    except ImportError:
        return None

    return _TracingDependencies(
        trace_api=trace_api,
        propagate_api=propagate_api,
        resource_cls=Resource,
        register_fn=register_fn,
        trace_context_propagator_cls=TraceContextTextMapPropagator,
    )


def configure_tracing_from_env(
    *,
    service_name: str,
    environ: Mapping[str, str] | None = None,
) -> TracingBootstrapResult:
    """Bootstrap OTEL + OpenInference tracing when explicitly enabled."""
    resolved_env = os.environ if environ is None else environ
    if not _is_enabled(resolved_env.get(ENABLE_TRACING_ENV_VAR)):
        return TracingBootstrapResult(enabled=False, configured=False, reason="disabled")

    project_name = resolve_tracing_project_name(resolved_env)
    otlp_endpoint = resolve_otel_tracing_endpoint(resolved_env)

    global _bootstrap_complete
    with _bootstrap_lock:
        if _bootstrap_complete:
            return TracingBootstrapResult(
                enabled=True,
                configured=True,
                reason="already_configured",
                project_name=project_name,
                otlp_endpoint=otlp_endpoint,
            )

        deps = _load_tracing_dependencies()
        if deps is None:
            logger.warning(
                "Tracing was enabled but observability dependencies are missing. "
                "Install openinference/opentelemetry packages to activate tracing.",
                extra={"service_name": service_name},
            )
            return TracingBootstrapResult(
                enabled=True,
                configured=False,
                reason="missing_dependencies",
                project_name=project_name,
                otlp_endpoint=otlp_endpoint,
            )

        # Create a resource to preserve the logical service name.
        resource = deps.resource_cls.create(
            {
                "service.name": service_name,
                "service.version": "0.1.0",
            }
        )

        # Use phoenix.otel.register() for simplified bootstrap and auto-instrumentation.
        # batch=False (default) uses SimpleSpanProcessor (ideal for API immediate export).
        # batch=True uses BatchSpanProcessor (ideal for Worker performance).
        deps.register_fn(
            project_name=project_name,
            endpoint=otlp_endpoint,
            resource=resource,
            batch=(service_name != "code-agent-api"),
            auto_instrument=True,
        )

        # Ensure TraceContextTextMapPropagator is the global propagator for cross-service linkage.
        deps.propagate_api.set_global_textmap(deps.trace_context_propagator_cls())

        _bootstrap_complete = True

    logger.info(
        "Tracing bootstrap completed for service runtime.",
        extra={
            "service_name": service_name,
            "project_name": project_name,
            "otlp_endpoint": otlp_endpoint,
        },
    )
    return TracingBootstrapResult(
        enabled=True,
        configured=True,
        reason="configured",
        project_name=project_name,
        otlp_endpoint=otlp_endpoint,
    )
