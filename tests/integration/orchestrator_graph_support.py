"""Shared test doubles for orchestrator graph integration tests."""

from __future__ import annotations

import asyncio

from orchestrator import WorkerResult
from workers import Worker, WorkerRequest


class StaticWorker(Worker):
    """Test worker that returns a predefined result and records requests."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


class SequencedWorker(Worker):
    """Test worker that yields a predefined sequence of results."""

    def __init__(self, results: list[WorkerResult]) -> None:
        self._results = list(results)
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        if not self._results:
            raise AssertionError("SequencedWorker received more requests than expected.")
        return self._results.pop(0)


class UnexpectedWorker(Worker):
    """Test worker that should never be invoked."""

    def __init__(self, message: str) -> None:
        self.message = message

    async def run(self, request: WorkerRequest) -> WorkerResult:
        raise AssertionError(self.message)


class SlowWorker(Worker):
    """Test worker that can be timed out or cancelled by the orchestrator."""

    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.requests: list[WorkerRequest] = []
        self.cancelled = False

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Slow worker finished.",
        )


class CrashingWorker(Worker):
    """Test worker that raises an unexpected exception before returning a result."""

    def __init__(self, message: str = "worker crashed") -> None:
        self.message = message
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        raise RuntimeError(self.message)


class CleanupCrashingWorker(Worker):
    """Test worker that raises during cancellation cleanup."""

    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.requests: list[WorkerRequest] = []
        self.cleanup_failed = False

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        try:
            await asyncio.sleep(self.delay_seconds)
        except asyncio.CancelledError as exc:
            self.cleanup_failed = True
            raise RuntimeError("cleanup failed after cancellation") from exc
        return WorkerResult(
            status="success",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="persist_memory",
            summary="Cleanup worker finished.",
        )
