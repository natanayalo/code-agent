"""Shared OpenInference LLM span helpers for runtime adapters."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from apps.observability import (
    SPAN_KIND_LLM,
    set_span_input_output,
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
        yield


def set_llm_span_output(output_data: Any) -> None:
    """Record the output payload for the current active LLM span."""
    set_span_input_output(input_data=None, output_data=output_data)
