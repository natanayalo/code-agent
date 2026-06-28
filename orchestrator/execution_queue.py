"""Background queue worker for execution-path orchestration."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from uuid import uuid4

from db.enums import WorkerNodeStatus
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
        capacity: int = 1,
        process_identity: str | None = None,
    ) -> None:
        self.service = service
        self.worker_id = worker_id or f"worker-{uuid4().hex[:8]}"
        self.poll_interval_seconds = max(0.25, poll_interval_seconds)
        self.lease_seconds = max(15, lease_seconds)
        self.capacity = max(1, capacity)
        self.heartbeat_interval_seconds = _heartbeat_interval_seconds(
            lease_seconds=self.lease_seconds
        )
        self.stale_worker_seconds = max(
            self.lease_seconds * 2,
            int(self.heartbeat_interval_seconds * 3),
        )
        self.process_identity = process_identity or f"{socket.gethostname()}:{os.getpid()}"

    @staticmethod
    def _should_stop_claiming(status: WorkerNodeStatus | None) -> bool:
        return status in {
            None,
            WorkerNodeStatus.DRAINING,
            WorkerNodeStatus.OFFLINE,
            WorkerNodeStatus.QUARANTINED,
        }

    async def run_forever(self) -> None:
        """Poll for queued tasks indefinitely."""
        logger.info(
            "Starting task queue worker loop",
            extra={
                "worker_id": self.worker_id,
                "poll_interval_seconds": self.poll_interval_seconds,
                "lease_seconds": self.lease_seconds,
                "capacity": self.capacity,
                "process_identity": self.process_identity,
            },
        )
        async with self.service:
            status = await self.service._run_blocking(
                self.service.register_worker_node,
                worker_id=self.worker_id,
                capacity=self.capacity,
                process_identity=self.process_identity,
            )
            if self._should_stop_claiming(status):
                logger.warning(
                    "Task queue worker registration returned non-claiming status",
                    extra={"worker_id": self.worker_id, "status": status.value if status else None},
                )
                return

            loop = asyncio.get_running_loop()
            next_heartbeat_at = loop.time() + self.heartbeat_interval_seconds
            next_sweep_at = loop.time()
            while True:
                try:
                    now = loop.time()
                    if now >= next_heartbeat_at:
                        status = await self.service._run_blocking(
                            self.service.heartbeat_worker_node,
                            worker_id=self.worker_id,
                        )
                        next_heartbeat_at = now + self.heartbeat_interval_seconds
                        if self._should_stop_claiming(status):
                            logger.warning(
                                "Task queue worker stopping claims due to registry status",
                                extra={
                                    "worker_id": self.worker_id,
                                    "status": status.value if status else None,
                                },
                            )
                            return
                    if now >= next_sweep_at:
                        await self.service._run_blocking(
                            self.service.sweep_worker_nodes,
                            stale_seconds=self.stale_worker_seconds,
                        )
                        next_sweep_at = now + self.lease_seconds

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
                except Exception as exc:
                    logger.exception("Transient error in task queue worker loop: %s", exc)
                    await asyncio.sleep(self.poll_interval_seconds)


__all__ = ["TaskQueueWorker", "_heartbeat_interval_seconds"]
