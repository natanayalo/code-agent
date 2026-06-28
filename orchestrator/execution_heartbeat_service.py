"""Lease heartbeat helpers for queued task execution."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from db.base import utc_now
from db.enums import WorkerNodeStatus
from orchestrator.execution_policy import _heartbeat_interval_seconds
from repositories import TaskRepository, WorkerNodeRepository, session_scope

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
        ok, worker_status = await self._run_blocking(
            self._heartbeat_task_and_worker,
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


def _heartbeat_task_and_worker(
    self: Any,
    *,
    task_id: str,
    worker_id: str,
    lease_seconds: int,
) -> tuple[bool, WorkerNodeStatus | None]:
    now = utc_now()
    with session_scope(self.session_factory) as session:
        task_ok = TaskRepository(session).heartbeat_lease(
            task_id=task_id,
            worker_id=worker_id,
            now=now,
            lease_seconds=lease_seconds,
        )
        worker_status = WorkerNodeRepository(session).heartbeat(worker_id=worker_id, now=now)
        return task_ok, worker_status
