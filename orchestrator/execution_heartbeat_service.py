"""Lease heartbeat helpers for queued task execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from db.enums import WorkerNodeStatus
from orchestrator.execution_policy import _heartbeat_interval_seconds

logger = logging.getLogger("orchestrator.execution")

_ABORTING_WORKER_STATUSES = {
    WorkerNodeStatus.OFFLINE,
    WorkerNodeStatus.QUARANTINED,
}


async def _heartbeat_loop(
    self: Any,
    *,
    task_id: str,
    worker_id: str,
    lease_seconds: int,
) -> None:
    """Best-effort lease heartbeat while task execution is in progress."""
    sleep_seconds = _heartbeat_interval_seconds(lease_seconds=lease_seconds)
    while True:
        await asyncio.sleep(sleep_seconds)
        ok = await self._run_blocking(
            self._heartbeat_task_lease,
            task_id=task_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if not ok:
            logger.debug(
                "Heartbeat failed: lease lost or task status changed",
                extra={"task_id": task_id, "worker_id": worker_id},
            )
            return None
        worker_status = await self._run_blocking(
            self.heartbeat_worker_node,
            worker_id=worker_id,
        )
        if worker_status is None or worker_status in _ABORTING_WORKER_STATUSES:
            logger.warning(
                "Heartbeat failed: worker node is not claimable",
                extra={
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "worker_status": getattr(worker_status, "value", worker_status)
                    if worker_status
                    else None,
                },
            )
            return None
