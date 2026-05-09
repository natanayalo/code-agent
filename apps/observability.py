"""OpenTelemetry/OpenInference tracing bootstrap for app runtimes."""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from functools import wraps
from importlib import import_module
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
TASK_ID_ATTRIBUTE: Final[str] = "code_agent.task_id"
ATTEMPT_COUNT_ATTRIBUTE: Final[str] = "code_agent.attempt_count"
CHANNEL_ATTRIBUTE: Final[str] = "code_agent.channel"
OUTCOME_STATUS_ATTRIBUTE: Final[str] = "code_agent.outcome_status"
ATTR_TASK_KIND: Final[str] = "code_agent.task_kind"
ATTR_WORKER_ID: Final[str] = "code_agent.worker_id"
MAX_SPAN_ATTRIBUTE_LENGTH: Final[int] = 12000

# Native Agent Span Attributes
NATIVE_AGENT_COMMAND_ATTRIBUTE: Final[str] = "code_agent.native_agent.command"
NATIVE_AGENT_EXIT_CODE_ATTRIBUTE: Final[str] = "code_agent.native_agent.exit_code"
NATIVE_AGENT_TIMED_OUT_ATTRIBUTE: Final[str] = "code_agent.native_agent.timed_out"
NATIVE_AGENT_STDOUT_ATTRIBUTE: Final[str] = "code_agent.native_agent.stdout"
NATIVE_AGENT_STDERR_ATTRIBUTE: Final[str] = "code_agent.native_agent.stderr"
NATIVE_AGENT_DURATION_ATTRIBUTE: Final[str] = "code_agent.native_agent.duration_seconds"
NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH: Final[int] = 2000

# Native Agent Resource Limits
DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS: Final[int] = 64 * 1024
DEFAULT_FINAL_MESSAGE_READ_BUFFER: Final[int] = DEFAULT_FINAL_MESSAGE_FILE_READ_MAX_CHARACTERS + 1

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


def is_tracing_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Check if tracing is explicitly enabled in the environment."""
    env = os.environ if environ is None else environ
    value = env.get(ENABLE_TRACING_ENV_VAR)
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
        from opentelemetry import propagate as propagate_api  # type: ignore  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # type: ignore  # noqa: PLC0415

        tracecontext_module = import_module("opentelemetry.trace.propagation.tracecontext")
        TraceContextTextMapPropagator = tracecontext_module.TraceContextTextMapPropagator
        from phoenix.otel import register as register_fn  # type: ignore  # noqa: PLC0415
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
    if not is_tracing_enabled(resolved_env):
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

    from opentelemetry import context as context_api  # type: ignore  # noqa: PLC0415

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

    from opentelemetry import context as context_api  # type: ignore  # noqa: PLC0415

    token = deps.propagate_api.extract(carrier=context)
    return context_api.attach(token)


def detach_trace_context(token: Any) -> None:
    """Detach a previously attached OpenTelemetry context token."""
    if token is None:
        return
    try:
        from opentelemetry import context as context_api  # type: ignore  # noqa: PLC0415

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


def bind_current_trace_context(  # noqa: UP047
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


def get_centralized_span_input_data(
    *,
    task_id: str | None = None,
    session_id: str | None = None,
    attempt: int | None = None,
    channel: str | None = None,
    extra_attributes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Consolidate standard task correlation attributes into a span attribute dictionary."""
    attributes = dict(extra_attributes) if extra_attributes is not None else {}
    if task_id:
        attributes[TASK_ID_ATTRIBUTE] = task_id
    if session_id:
        attributes[SESSION_ID_ATTRIBUTE] = session_id
    if attempt is not None:
        attributes[ATTEMPT_COUNT_ATTRIBUTE] = attempt
    if channel:
        attributes[CHANNEL_ATTRIBUTE] = channel
    return attributes


def get_centralized_result_mapping() -> dict[str, Any] | None:
    """Return the centralized mapping of string statuses to OpenTelemetry StatusCode enum values."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

        return {
            "success": otel_trace.StatusCode.OK,
            "completed": otel_trace.StatusCode.OK,
            "ok": otel_trace.StatusCode.OK,
            "error": otel_trace.StatusCode.ERROR,
            "failure": otel_trace.StatusCode.ERROR,
            "failed": otel_trace.StatusCode.ERROR,
            "cancelled": otel_trace.StatusCode.ERROR,
            "unset": otel_trace.StatusCode.UNSET,
        }
    except ImportError:
        return None


def _resolve_span_status_code(status: str) -> Any:
    """Map a string status to an OpenTelemetry StatusCode enum value."""
    try:
        mapping = get_centralized_result_mapping()
        if mapping is None:
            return None
        return mapping.get(status.lower(), mapping["unset"])
    except Exception as exc:
        logger.debug("Failed to resolve span status code: %s", exc)
        return None


def get_centralized_span_status(
    status: str,
    description: str | None = None,
) -> Any:
    """Map a standard outcome status (success/error/failure) to an OpenTelemetry Status object."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

        status_code = _resolve_span_status_code(status)
        if status_code is not None:
            return otel_trace.Status(status_code, description)

        return None
    except Exception as exc:
        logger.debug("Failed to map span status: %s", exc)
        return None


def start_optional_span(
    *,
    tracer_name: str,
    span_name: str,
    attributes: Mapping[str, Any] | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    attempt: int | None = None,
    channel: str | None = None,
) -> Any:
    """Start a span when OTEL is available, otherwise return a no-op context manager."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

        tracer = otel_trace.get_tracer(tracer_name)
        span_attributes = get_centralized_span_input_data(
            task_id=task_id,
            session_id=session_id,
            attempt=attempt,
            channel=channel,
            extra_attributes=attributes,
        )

        return tracer.start_as_current_span(span_name, attributes=span_attributes)
    except (ImportError, Exception):
        return nullcontext()


def with_span_kind(kind: str, attributes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return span attributes with a standardized OpenInference span-kind key."""
    merged = dict(attributes) if attributes is not None else {}
    merged[OPENINFERENCE_SPAN_KIND_ATTRIBUTE] = kind
    return merged


def _truncate_span_payload(value: str) -> str:
    """Standardized truncation for span attributes to prevent oversized payloads."""
    if len(value) <= MAX_SPAN_ATTRIBUTE_LENGTH:
        return value
    truncated = value[:MAX_SPAN_ATTRIBUTE_LENGTH]
    return f"{truncated}\n... (truncated to {MAX_SPAN_ATTRIBUTE_LENGTH} chars)"


def _serialize_span_payload(payload: Any) -> tuple[str, str]:
    """Serialize payloads for OpenInference input/output span attributes."""
    if isinstance(payload, Mapping | list | tuple):
        actual_payload = dict(payload) if isinstance(payload, Mapping) else payload
        serialized = json.dumps(actual_payload, default=str)
        mime_type = "application/json"
    else:
        serialized = str(payload)
        mime_type = "text/plain"

    truncated = _truncate_span_payload(serialized)
    if len(serialized) > MAX_SPAN_ATTRIBUTE_LENGTH:
        mime_type = "text/plain"

    return truncated, mime_type


def set_span_input_output(
    input_data: Any,
    output_data: Any = None,
) -> None:
    """Set OpenInference input/output attributes on the current span."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

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
    except Exception as exc:
        # Fail safe if tracing is not configured or JSON fails
        logger.debug("Failed to set span input/output: %s", exc)


def set_optional_span_attribute(span: Any, key: str, value: Any) -> None:
    """Best-effort span attribute setter that tolerates no-op span contexts."""
    if span is None:
        return
    try:
        if hasattr(span, "is_recording") and not span.is_recording():
            return
        if hasattr(span, "set_attribute"):
            span.set_attribute(key, value)
    except Exception as exc:
        logger.debug("Failed to set optional span attribute '%s': %s", key, exc)


def set_current_span_attribute(key: str, value: Any) -> None:
    """Set a single attribute on the current span when tracing is available."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

        span = otel_trace.get_current_span()
        set_optional_span_attribute(span, key, value)
    except Exception as exc:
        logger.debug("Failed to set current span attribute '%s': %s", key, exc)


def record_span_exception(exc: Exception) -> None:
    """Record an exception on the current span when tracing is available."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

        span = otel_trace.get_current_span()
        if span.is_recording():
            span.record_exception(exc)
    except Exception as tracing_exc:
        logger.debug("Failed to record span exception: %s", tracing_exc)


def set_span_status(status_code: Any, description: str | None = None) -> None:
    """Set the status of the current span when tracing is available."""
    try:
        from opentelemetry import trace as otel_trace  # type: ignore  # noqa: PLC0415

        span = otel_trace.get_current_span()
        if span.is_recording():
            if isinstance(status_code, str):
                status_code = _resolve_span_status_code(status_code)

            if status_code is None:
                return

            # If we don't have a Status object yet, create one
            # We check for .status_code attribute which is standard on OTEL Status objects
            if not hasattr(status_code, "status_code"):
                status_code = otel_trace.Status(status_code, description)

            span.set_status(status_code)
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("Failed to set span status: %s", exc)


def set_span_status_from_outcome(status: str, summary: str | None = None) -> None:
    """Set span status based on a standard outcome status (success/error/failure)."""
    set_current_span_attribute(OUTCOME_STATUS_ATTRIBUTE, status)
    status_obj = get_centralized_span_status(status, summary)
    if status_obj:
        set_span_status(status_obj)


def set_span_task_metadata(
    task_id: str | None = None,
    session_id: str | None = None,
    attempt: int | None = None,
    channel: str | None = None,
) -> None:
    """Set standardized task correlation attributes on the current span."""
    if task_id:
        set_current_span_attribute(TASK_ID_ATTRIBUTE, task_id)
    if session_id:
        set_current_span_attribute(SESSION_ID_ATTRIBUTE, session_id)
    if attempt is not None:
        set_current_span_attribute(ATTEMPT_COUNT_ATTRIBUTE, attempt)
    if channel:
        set_current_span_attribute(CHANNEL_ATTRIBUTE, channel)
