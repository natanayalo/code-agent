"""Legacy queue ownership guards for orchestration-runtime routing."""

from __future__ import annotations

import logging
from typing import Any

from repositories import TaskRepository, WorkerNodeRepository, session_scope

logger = logging.getLogger("orchestrator.execution")


async def reject_nonlegacy_queued_task(
    self: Any,
    *,
    task_id: str,
    worker_id: str,
    orchestration_runtime: str | None,
) -> None:
    """Release an accidental legacy claim without executing a non-legacy task."""
    logger.error(
        "Legacy worker refused task with non-legacy runtime ownership",
        extra={
            "task_id": task_id,
            "worker_id": worker_id,
            "runtime": orchestration_runtime,
        },
    )
    await self._run_blocking(
        _release_legacy_ownership_violation,
        self,
        task_id=task_id,
        worker_id=worker_id,
    )


async def legacy_worker_may_execute(
    self: Any,
    *,
    task_id: str,
    worker_id: str,
    orchestration_runtime: str | None,
) -> bool:
    """Return whether the legacy worker owns the task, releasing invalid claims."""
    if orchestration_runtime == "legacy":
        return True
    await reject_nonlegacy_queued_task(
        self,
        task_id=task_id,
        worker_id=worker_id,
        orchestration_runtime=orchestration_runtime,
    )
    return False


def _release_legacy_ownership_violation(self: Any, *, task_id: str, worker_id: str) -> None:
    """Return a non-legacy task and release its worker capacity reservation."""
    with session_scope(self.session_factory) as session:
        released = TaskRepository(session).release_runtime_ownership_violation(
            task_id=task_id,
            worker_id=worker_id,
        )
        if released:
            WorkerNodeRepository(session).release_load(worker_id=worker_id)
