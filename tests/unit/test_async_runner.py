"""Unit tests for executor/cancellation worker helpers."""

from __future__ import annotations

import asyncio
import time

import pytest

from workers import async_runner as async_runner_module


def test_run_sync_with_cancellable_executor_returns_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executor helper should return sync results and pass through trace binding."""
    bind_calls: list[object] = []
    cancel_snapshots: list[bool] = []

    def _bind(func):
        bind_calls.append(func)
        return func

    monkeypatch.setattr(async_runner_module, "bind_current_trace_context", _bind)

    def _sync_work(cancel_requested):
        cancel_snapshots.append(cancel_requested())
        return "ok"

    async def _run() -> str:
        return await async_runner_module.run_sync_with_cancellable_executor(_sync_work)

    result = asyncio.run(_run())
    assert result == "ok"
    assert cancel_snapshots == [False]
    assert len(bind_calls) == 1


def test_run_sync_with_cancellable_executor_gracefully_finishes_after_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation should set the cancel token and wait for graceful sync completion."""
    monkeypatch.setattr(async_runner_module, "bind_current_trace_context", lambda func: func)

    def _sync_work(cancel_requested):
        while not cancel_requested():
            time.sleep(0.01)
        return "stopped-cleanly"

    async def _scenario() -> str:
        task = asyncio.create_task(
            async_runner_module.run_sync_with_cancellable_executor(_sync_work)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    result = asyncio.run(_scenario())
    assert result == "stopped-cleanly"


def test_run_sync_with_cancellable_executor_raises_when_graceful_shutdown_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation should raise when sync code ignores cancel requests for too long."""
    monkeypatch.setattr(async_runner_module, "bind_current_trace_context", lambda func: func)

    def _sync_work(_cancel_requested):
        time.sleep(0.2)
        return "late"

    async def _scenario() -> None:
        task = asyncio.create_task(
            async_runner_module.run_sync_with_cancellable_executor(
                _sync_work,
                cancellation_timeout_seconds=0.01,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(
            asyncio.CancelledError,
            match="Graceful shutdown of sync worker timed out.",
        ):
            await task

    asyncio.run(_scenario())
