"""Unit tests for queue worker polling cadence."""

from __future__ import annotations

import asyncio

import pytest

from db.enums import WorkerNodeStatus
from orchestrator.execution_queue import TaskQueueWorker


class _StopQueueWorker(BaseException):
    """Stop the infinite queue loop once the assertion window is covered."""


class _FakeLoop:
    def __init__(self) -> None:
        self.current_time = 0.0

    def time(self) -> float:
        return self.current_time


class _FakeQueueService:
    def __init__(self, loop: _FakeLoop) -> None:
        self.loop = loop
        self.sweep_times: list[float] = []

    async def __aenter__(self) -> _FakeQueueService:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    async def _run_blocking(self, func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    def register_worker_node(self, **kwargs: object) -> WorkerNodeStatus:
        return WorkerNodeStatus.ACTIVE

    def heartbeat_worker_node(self, **kwargs: object) -> WorkerNodeStatus:
        return WorkerNodeStatus.ACTIVE

    def sweep_worker_nodes(self, **kwargs: object) -> dict[str, int]:
        self.sweep_times.append(self.loop.time())
        return {"reclaimed_leases": 0, "stale_workers": 0}

    def claim_next_task(self, **kwargs: object) -> None:
        if self.loop.time() >= 30:
            raise _StopQueueWorker()
        return None


@pytest.mark.anyio
async def test_queue_worker_sweeps_at_lease_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale-worker sweeps should run on lease cadence, not heartbeat cadence."""
    fake_loop = _FakeLoop()
    service = _FakeQueueService(fake_loop)

    async def fake_sleep(delay: float) -> None:
        fake_loop.current_time += 10.0

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: fake_loop)
    monkeypatch.setattr("orchestrator.execution_queue.asyncio.sleep", fake_sleep)

    queue_worker = TaskQueueWorker(
        service=service,
        worker_id="worker-cadence",
        poll_interval_seconds=1.0,
        lease_seconds=30,
    )

    with pytest.raises(_StopQueueWorker):
        await queue_worker.run_forever()

    assert service.sweep_times == [0.0, 30.0]
