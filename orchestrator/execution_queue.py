"""Background queue worker for execution-path orchestration."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from orchestrator.execution_policy import _heartbeat_interval_seconds

logger = logging.getLogger(__name__)


class TaskQueueWorker:
    """Long-running queue poller that claims and executes queued tasks."""

    def __init__(  # type: ignore[no-untyped-def]
        self,
        *,
        service,
        worker_id: str | None = None,
        poll_interval_seconds: float = 2.0,
        lease_seconds: int = 60,
    ) -> None:
        self.service = service
        self.worker_id = worker_id or f"worker-{uuid4().hex[:8]}"
        self.poll_interval_seconds = max(0.25, poll_interval_seconds)
        self.lease_seconds = max(15, lease_seconds)

    async def run_forever(self) -> None:
        """Poll for queued tasks indefinitely."""
        logger.info(
            "Starting task queue worker loop",
            extra={
                "worker_id": self.worker_id,
                "poll_interval_seconds": self.poll_interval_seconds,
                "lease_seconds": self.lease_seconds,
            },
        )
        async with self.service:
            while True:
                claim = await self.service._run_blocking(
                    self.service.claim_next_task,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
                if claim is None:
                    await asyncio.sleep(self.poll_interval_seconds)
                    continue
                await self.service.run_queued_task(
                    task_id=claim.task_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )


__all__ = ["TaskQueueWorker", "_heartbeat_interval_seconds"]
