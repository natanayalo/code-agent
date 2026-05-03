"""Unit tests for shared LLM span helpers."""

from __future__ import annotations

from contextlib import contextmanager

from workers import llm_tracing as llm_tracing_module


def test_with_llm_span_sets_kind_and_input(monkeypatch) -> None:
    """LLM span helper should open a span and record input payload attributes."""
    recorded: dict[str, object] = {}
    io_calls: list[dict[str, object]] = []

    def _with_span_kind(kind: str) -> dict[str, str]:
        recorded["span_kind"] = kind
        return {"openinference.span.kind": kind}

    @contextmanager
    def _start_optional_span(*, tracer_name: str, span_name: str, attributes):
        recorded["tracer_name"] = tracer_name
        recorded["span_name"] = span_name
        recorded["attributes"] = attributes
        recorded["entered"] = True
        try:
            yield
        finally:
            recorded["exited"] = True

    def _set_span_input_output(*, input_data=None, output_data=None) -> None:
        io_calls.append({"input_data": input_data, "output_data": output_data})

    monkeypatch.setattr(llm_tracing_module, "with_span_kind", _with_span_kind)
    monkeypatch.setattr(llm_tracing_module, "start_optional_span", _start_optional_span)
    monkeypatch.setattr(llm_tracing_module, "set_span_input_output", _set_span_input_output)

    with llm_tracing_module.with_llm_span(
        tracer_name="workers.test",
        span_name="test.chat",
        input_data={"prompt": "hello"},
    ):
        recorded["body_ran"] = True

    assert recorded["span_kind"] == llm_tracing_module.SPAN_KIND_LLM
    assert recorded["tracer_name"] == "workers.test"
    assert recorded["span_name"] == "test.chat"
    assert recorded["attributes"] == {"openinference.span.kind": llm_tracing_module.SPAN_KIND_LLM}
    assert recorded["entered"] is True
    assert recorded["body_ran"] is True
    assert recorded["exited"] is True
    assert io_calls == [{"input_data": {"prompt": "hello"}, "output_data": None}]


def test_set_llm_span_output_sets_output_only(monkeypatch) -> None:
    """Output helper should write output payload without replacing span input."""
    io_calls: list[dict[str, object]] = []

    def _set_span_input_output(*, input_data=None, output_data=None) -> None:
        io_calls.append({"input_data": input_data, "output_data": output_data})

    monkeypatch.setattr(llm_tracing_module, "set_span_input_output", _set_span_input_output)

    llm_tracing_module.set_llm_span_output({"response": "done"})

    assert io_calls == [{"input_data": None, "output_data": {"response": "done"}}]
