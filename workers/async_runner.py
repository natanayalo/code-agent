"""Helpers for running sync worker code in executor threads safely."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import TypeVar

from apps.observability import bind_current_trace_context

DEFAULT_CANCELLATION_TIMEOUT_SECONDS = 10.0
DEFAULT_CANCELLATION_TIMEOUT_MESSAGE = "Graceful shutdown of sync worker timed out."
T = TypeVar("T")


async def run_sync_with_cancellable_executor(
    run_sync: Callable[[Callable[[], bool]], T],
    *,
    cancellation_timeout_seconds: float = DEFAULT_CANCELLATION_TIMEOUT_SECONDS,
    cancellation_timeout_message: str = DEFAULT_CANCELLATION_TIMEOUT_MESSAGE,
) -> T:
    """Run sync work in a thread executor with cancellation-aware graceful shutdown.

    `run_sync` receives a callable `cancel_requested()` and should poll it for
    graceful cancellation when possible.
    """
    cancel_event = threading.Event()
    loop = asyncio.get_running_loop()

    def _run() -> T:
        return run_sync(cancel_event.is_set)

    future = loop.run_in_executor(None, bind_current_trace_context(_run))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=cancellation_timeout_seconds,
            )
        except TimeoutError as exc:
            raise asyncio.CancelledError(cancellation_timeout_message) from exc
