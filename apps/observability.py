"""OpenTelemetry/OpenInference tracing bootstrap for app runtimes."""

from __future__ import annotations

import importlib.metadata
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from threading import Lock
from typing import Any, Final, TypeVar

logger = logging.getLogger(__name__)

ENABLE_TRACING_ENV_VAR: Final[str] = "CODE_AGENT_ENABLE_TRACING"
TRACING_PROJECT_ENV_VAR: Final[str] = "CODE_AGENT_TRACING_PROJECT"
TRACING_OTLP_ENDPOINT_ENV_VAR: Final[str] = "CODE_AGENT_TRACING_OTLP_ENDPOINT"
OTEL_OTLP_TRACES_ENDPOINT_ENV_VAR: Final[str] = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
PHOENIX_COLLECTOR_ENDPOINT_ENV_VAR: Final[str] = "PHOENIX_COLLECTOR_ENDPOINT"
DEFAULT_TRACING_PROJECT: Final[str] = "code-agent"
DEFAULT_PHOENIX_OTLP_HTTP_ENDPOINT: Final[str] = "http://127.0.0.1:6006/v1/traces"
OPENINFERENCE_SPAN_KIND_ATTRIBUTE: Final[str] = "openinference.span.kind"
SESSION_ID_ATTRIBUTE: Final[str] = "session.id"
INPUT_VALUE_ATTRIBUTE: Final[str] = "input.value"
INPUT_MIME_TYPE_ATTRIBUTE: Final[str] = "input.mime_type"
OUTPUT_VALUE_ATTRIBUTE: Final[str] = "output.value"
OUTPUT_MIME_TYPE_ATTRIBUTE: Final[str] = "output.mime_type"
SPAN_KIND_AGENT: Final[str] = "AGENT"
SPAN_KIND_CHAIN: Final[str] = "CHAIN"
SPAN_KIND_LLM: Final[str] = "LLM"
SPAN_KIND_TOOL: Final[str] = "TOOL"
STATUS_OK: Final[str] = "OK"
STATUS_ERROR: Final[str] = "ERROR"
STATUS_UNSET: Final[str] = "UNSET"
TRACE_CONTEXT_ENV_KEYS: Final[dict[str, str]] = {
    "traceparent": "TRACEPARENT",
    "tracestate": "TRACESTATE",
    "baggage": "BAGGAGE",
}
IMMEDIATE_EXPORT_SERVICES: Final[frozenset[str]] = frozenset({"code-agent-api"})
DEFAULT_SERVICE_VERSION: Final[str] = "0.1.0-dev"

_bootstrap_lock = Lock()
_bootstrap_complete = False

try:
    _version = importlib.metadata.version("code-agent")
except importlib.metadata.PackageNotFoundError:
    _version = DEFAULT_SERVICE_VERSION
_SERVICE_VERSION: Final[str] = _version
T = TypeVar("T")


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
        from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
        from opentelemetry.trace.propagation.tracecontext import (  # type: ignore[import-not-found]
            TraceContextTextMapPropagator,
        )
        from phoenix.otel import register as register_fn  # type: ignore[import-not-found]
    except ImportError:
        return None

    return _TracingDependencies(
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
        service_version = _SERVICE_VERSION

        resource = deps.resource_cls.create(
            {
                "service.name": service_name,
                "service.version": service_version,
                "openinference.project.name": project_name,
            }
        )

        # Use phoenix.otel.register() for simplified bootstrap and auto-instrumentation.
        # batch=False (default) uses SimpleSpanProcessor (ideal for API immediate export).
        # batch=True uses BatchSpanProcessor (ideal for Worker performance).
        deps.register_fn(
            project_name=project_name,
            endpoint=otlp_endpoint,
            resource=resource,
            batch=(service_name not in IMMEDIATE_EXPORT_SERVICES),
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


def capture_trace_context() -> dict[str, str]:
    """Capture the current OpenTelemetry trace context into a serializable dict."""
    deps = _load_tracing_dependencies()
    if deps is None:
        return {}

    from opentelemetry import context as context_api  # type: ignore[import-not-found]

    carrier: dict[str, str] = {}
    deps.propagate_api.inject(carrier, context=context_api.get_current())
    return carrier


def inject_w3c_trace_context_env(
    base_environment: Mapping[str, str] | None = None,
    *,
    trace_context: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge W3C trace context into process environment keys when available."""
    effective_env = {} if base_environment is None else dict(base_environment)
    resolved_trace_context = capture_trace_context() if trace_context is None else trace_context
    for context_key, env_key in TRACE_CONTEXT_ENV_KEYS.items():
        value = resolved_trace_context.get(context_key)
        if value:
            effective_env.setdefault(env_key, value)
    return effective_env


def restore_trace_context(context: dict[str, str] | None) -> Any:
    """Restore an OpenTelemetry trace context from a serializable dict.

    Returns a token that should be detached later if used in a context manager,
    or None if tracing is disabled.
    """
    if not context:
        return None

    deps = _load_tracing_dependencies()
    if deps is None:
        return None

    from opentelemetry import context as context_api  # type: ignore[import-not-found]

    token = deps.propagate_api.extract(carrier=context)
    return context_api.attach(token)


def detach_trace_context(token: Any) -> None:
    """Detach a previously attached OpenTelemetry context token."""
    if token is None:
        return
    try:
        from opentelemetry import context as context_api  # type: ignore[import-not-found]

        context_api.detach(token)
    except ImportError:
        pass
    except (RuntimeError, AttributeError):
        logger.debug("Failed to detach OpenTelemetry context", exc_info=True)


@contextmanager
def with_restored_trace_context(context: dict[str, str] | None) -> Iterator[None]:
    """Context manager that restores and reliably detaches trace context."""
    token = restore_trace_context(context)
    try:
        yield
    finally:
        detach_trace_context(token)


def bind_current_trace_context(
    func: Callable[[], T],
    *,
    original_func: Callable[..., Any] | None = None,
) -> Callable[[], T]:
    """Bind current trace context to a callable for thread/executor handoff."""
    trace_context = capture_trace_context()

    @wraps(func)
    def _wrapped() -> T:
        with with_restored_trace_context(trace_context):
            return func()

    _wrapped.func = original_func if original_func is not None else getattr(func, "func", func)  # type: ignore[attr-defined]
    return _wrapped


def start_optional_span(
    *,
    tracer_name: str,
    span_name: str,
    attributes: Mapping[str, Any] | None = None,
) -> Any:
    """Start a span when OTEL is available, otherwise return a no-op context manager."""
    from contextlib import nullcontext

    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

        tracer = otel_trace.get_tracer(tracer_name)
        span_attributes = dict(attributes) if attributes is not None else None
        return tracer.start_as_current_span(span_name, attributes=span_attributes)
    except (ImportError, Exception):
        return nullcontext()


def with_span_kind(kind: str, attributes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return span attributes with a standardized OpenInference span-kind key."""
    merged = dict(attributes) if attributes is not None else {}
    merged[OPENINFERENCE_SPAN_KIND_ATTRIBUTE] = kind
    return merged


def _serialize_span_payload(payload: Any) -> tuple[str, str]:
    """Serialize payloads for OpenInference input/output span attributes."""
    import json

    if isinstance(payload, Mapping | list | tuple):
        actual_payload = dict(payload) if isinstance(payload, Mapping) else payload
        return json.dumps(actual_payload, default=str), "application/json"
    return str(payload), "text/plain"


def set_span_input_output(
    input_data: Any,
    output_data: Any = None,
) -> None:
    """Set OpenInference input/output attributes on the current span."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

        span = otel_trace.get_current_span()
        if not span.is_recording():
            return

        if input_data is not None:
            input_str, input_mime_type = _serialize_span_payload(input_data)
            span.set_attribute(INPUT_VALUE_ATTRIBUTE, input_str)
            span.set_attribute(INPUT_MIME_TYPE_ATTRIBUTE, input_mime_type)

        if output_data is not None:
            output_str, output_mime_type = _serialize_span_payload(output_data)
            span.set_attribute(OUTPUT_VALUE_ATTRIBUTE, output_str)
            span.set_attribute(OUTPUT_MIME_TYPE_ATTRIBUTE, output_mime_type)
    except (ImportError, Exception):
        # Fail safe if tracing is not configured or JSON fails
        pass


def set_optional_span_attribute(span: Any, key: str, value: Any) -> None:
    """Best-effort span attribute setter that tolerates no-op span contexts."""
    if span is None:
        return
    try:
        if hasattr(span, "is_recording") and not span.is_recording():
            return
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception:
        pass


def set_current_span_attribute(key: str, value: Any) -> None:
    """Set a single attribute on the current span when tracing is available."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

        span = otel_trace.get_current_span()
        set_optional_span_attribute(span, key, value)
    except (ImportError, Exception):
        pass


def record_span_exception(exc: Exception) -> None:
    """Record an exception on the current span when tracing is available."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

        span = otel_trace.get_current_span()
        if span.is_recording():
            span.record_exception(exc)
    except (ImportError, Exception):
        pass


def set_span_status(status_code: Any, description: str | None = None) -> None:
    """Set the status of the current span when tracing is available."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

        span = otel_trace.get_current_span()
        if span.is_recording():
            if isinstance(status_code, str):
                # Map string to StatusCode enum
                status_code = getattr(
                    otel_trace.StatusCode, status_code.upper(), otel_trace.StatusCode.UNSET
                )

            # If we don't have a Status object yet, create one
            # We check for .status_code attribute which is standard on OTEL Status objects
            if not hasattr(status_code, "status_code"):
                status_code = otel_trace.Status(status_code, description)

            span.set_status(status_code)
    except (ImportError, Exception):
        pass


def set_span_status_from_outcome(status: str, summary: str | None = None) -> None:
    """Set span status based on a standard outcome status (success/error/failure)."""
    if status == "success":
        set_span_status(STATUS_OK)
    else:
        set_span_status(STATUS_ERROR, summary)
