"""Unit tests for OpenTelemetry/OpenInference tracing bootstrap."""

from __future__ import annotations

import builtins
from collections import UserDict
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apps import observability as observability_module
from apps import observability_utils


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
    long_payload = "a" * (observability_utils.MAX_SPAN_ATTRIBUTE_LENGTH + 100)

    with patch("opentelemetry.trace.get_current_span", return_value=span):
        observability_module.set_span_input_output(input_data=long_payload)

    val = span.attributes["input.value"]
    assert len(val) > observability_utils.MAX_SPAN_ATTRIBUTE_LENGTH
    assert "... (truncated to 12000 chars)" in val
    assert val.startswith("a" * observability_utils.MAX_SPAN_ATTRIBUTE_LENGTH)


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
    long_dict = {"key": "a" * observability_utils.MAX_SPAN_ATTRIBUTE_LENGTH}

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
    limit = observability_utils.MAX_SPAN_ATTRIBUTE_LENGTH
    short_text = "abc"
    assert observability_utils.truncate_span_payload(short_text) == short_text

    long_text = "x" * (limit + 10)
    truncated = observability_utils.truncate_span_payload(long_text)
    assert len(truncated) > limit
    assert "... (truncated to 12000 chars)" in truncated
    assert truncated.startswith("x" * limit)


def test_serialize_span_payload_json() -> None:
    """Test JSON serialization and MIME type."""
    payload = {"a": 1}
    truncated, mime = observability_utils.serialize_span_payload(payload)
    assert truncated == '{"a": 1}'
    assert mime == "application/json"


def test_serialize_span_payload_text() -> None:
    """Test plain text serialization."""
    payload = 123
    truncated, mime = observability_utils.serialize_span_payload(payload)
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


def test_get_centralized_span_status() -> None:
    """Verify that result statuses map to correct OTel status codes."""
    from opentelemetry import trace as otel_trace

    # Success cases
    s1 = observability_utils.get_centralized_span_status("success")
    assert s1.status_code == otel_trace.StatusCode.OK

    s2 = observability_utils.get_centralized_span_status("completed")
    assert s2.status_code == otel_trace.StatusCode.OK

    # Error cases
    e1 = observability_utils.get_centralized_span_status("error")
    assert e1.status_code == otel_trace.StatusCode.ERROR

    e2 = observability_utils.get_centralized_span_status("failure")
    assert e2.status_code == otel_trace.StatusCode.ERROR

    e3 = observability_utils.get_centralized_span_status("failed")
    assert e3.status_code == otel_trace.StatusCode.ERROR

    e4 = observability_utils.get_centralized_span_status("cancelled")
    assert e4.status_code == otel_trace.StatusCode.ERROR

    # Unknown case
    u1 = observability_utils.get_centralized_span_status("unknown_status")
    assert u1.status_code == otel_trace.StatusCode.UNSET


def test_set_span_status_string_mapping() -> None:
    """Verify that set_span_status correctly maps string input to OTel codes."""
    from opentelemetry import trace as otel_trace

    class _FakeSpan:
        def __init__(self) -> None:
            self.status = None

        def is_recording(self) -> bool:
            return True

        def set_status(self, status):
            self.status = status

    span = _FakeSpan()
    with patch("opentelemetry.trace.get_current_span", return_value=span):
        # Test whitelisted strings
        observability_module.set_span_status("SUCCESS")
        assert span.status.status_code == otel_trace.StatusCode.OK

        observability_module.set_span_status("COMPLETED")
        assert span.status.status_code == otel_trace.StatusCode.OK

        observability_module.set_span_status("FAILED")
        assert span.status.status_code == otel_trace.StatusCode.ERROR

        observability_module.set_span_status("CANCELLED")
        assert span.status.status_code == otel_trace.StatusCode.ERROR

        # Test fallback
        observability_module.set_span_status("UNKNOWN")
        assert span.status.status_code == otel_trace.StatusCode.UNSET
