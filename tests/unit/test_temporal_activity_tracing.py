from __future__ import annotations

from contextlib import contextmanager

import pytest

from orchestrator.temporal.activities import _restore_task_trace_context


@pytest.mark.anyio
async def test_temporal_activity_restores_task_trace_context(monkeypatch) -> None:
    """Activity work must run under the trace context captured at task ingress."""
    seen_contexts: list[dict[str, str]] = []

    @contextmanager
    def record_context(trace_context: dict[str, str]):
        seen_contexts.append(trace_context)
        yield

    monkeypatch.setattr(
        "orchestrator.temporal.activities.with_restored_trace_context", record_context
    )

    class FakeService:
        async def _run_blocking(self, func, *args):
            return func(*args)

    class FakeActivity:
        service = FakeService()

        def _load_task_trace_context(self, task_id: str) -> dict[str, str]:
            assert task_id == "task-123"
            return {"traceparent": "00-abc-def-01"}

        @_restore_task_trace_context
        async def execute(self, task_id: str) -> str:
            assert task_id == "task-123"
            return "done"

    assert await FakeActivity().execute("task-123") == "done"
    assert seen_contexts == [{"traceparent": "00-abc-def-01"}]
