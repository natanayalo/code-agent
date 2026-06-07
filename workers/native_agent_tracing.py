from __future__ import annotations

from apps.observability import NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH, set_span_input_output
from sandbox.redact import SecretRedactor, redact_and_truncate_output


def _record_span_data(
    *,
    input_data: str | None = None,
    output_data: str | None = None,
    redactor: SecretRedactor | None = None,
) -> None:
    """Standardized recording of span input/output with redaction and truncation."""
    # Use consistent truncation limit for span attributes to prevent oversized payloads.
    limit = NATIVE_AGENT_TRACING_STREAM_MAX_LENGTH
    redacted_input = (
        redact_and_truncate_output(input_data, redactor=redactor, limit_chars=limit)
        if input_data is not None
        else None
    )
    redacted_output = (
        redact_and_truncate_output(output_data, redactor=redactor, limit_chars=limit)
        if output_data is not None
        else None
    )
    set_span_input_output(input_data=redacted_input, output_data=redacted_output)
