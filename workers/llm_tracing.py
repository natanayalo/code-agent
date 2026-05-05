"""Shared OpenInference LLM span helpers for runtime adapters."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from apps.observability import (
    SPAN_KIND_LLM,
    STATUS_ERROR,
    STATUS_OK,
    record_span_exception,
    set_span_input_output,
    set_span_status,
    start_optional_span,
    with_span_kind,
)


@contextmanager
def with_llm_span(
    *,
    tracer_name: str,
    span_name: str,
    input_data: Any,
) -> Iterator[None]:
    """Trace an LLM adapter turn and capture input payload attributes."""

    with start_optional_span(
        tracer_name=tracer_name,
        span_name=span_name,
        attributes=with_span_kind(SPAN_KIND_LLM),
    ):
        set_span_input_output(input_data=input_data)
        try:
            yield
            set_span_status(STATUS_OK)
        except Exception as exc:
            record_span_exception(exc)
            set_span_status(STATUS_ERROR, str(exc))
            raise


def set_llm_span_output(output_data: Any) -> None:
    """Record the output payload for the current active LLM span."""
    set_span_input_output(input_data=None, output_data=output_data)


def normalize_llm_output(output: Any) -> Any:
    """Unwrap structured LLM responses into plain text or normalized JSON for tracing."""
    if not isinstance(output, str):
        return output

    try:
        data = json.loads(output)
        if isinstance(data, dict) and "response" in data:
            val = data["response"]
            # If the inner response is stringified JSON, unwrap it again for visibility
            if isinstance(val, str):
                try:
                    return json.loads(val)
                except (json.JSONDecodeError, TypeError, ValueError):
                    return val
            return val
        return data
    except (json.JSONDecodeError, TypeError, ValueError):
        return output
