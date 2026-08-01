"""Execution-readiness policy and machine-readable operational signals."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Protocol, TypeVar

from temporalio.client import Client, WorkflowExecutionStatus

from db.base import utc_now
from db.enums import TaskStatus
from orchestrator.operational_health_types import (
    ExecutionHealthMetrics,
    InteractionWaitMetrics,
    OutboxHealthMetrics,
    ReadinessComponent,
    ReadinessSnapshot,
    TerminalReconciliationMetrics,
    WorkerHealthMetrics,
)
from repositories import session_scope
from repositories.sqlalchemy_operational_health import (
    InteractionOperationalSnapshot,
    OperationalHealthRepository,
    OutboxOperationalSnapshot,
    TemporalTaskProjection,
    WorkerOperationalSnapshot,
)

logger = logging.getLogger(__name__)

WORKER_HEARTBEAT_INTERVAL_SECONDS = 10
WORKER_FRESH_SECONDS = 30
OUTBOX_STALE_SECONDS = 60
INTERACTION_STUCK_SECONDS = 24 * 60 * 60
TERMINAL_RECONCILIATION_GRACE_SECONDS = 60
POSTGRES_PROBE_TIMEOUT_SECONDS = 2
TEMPORAL_PROBE_TIMEOUT_SECONDS = 2
AFFECTED_TASK_ID_LIMIT = 20

_TERMINAL_STATUSES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}
_TEMPORAL_RUNNING_STATUSES = {
    WorkflowExecutionStatus.RUNNING.name.lower(),
    WorkflowExecutionStatus.CONTINUED_AS_NEW.name.lower(),
}
_T = TypeVar("_T")


class TemporalOperationalProbeProtocol(Protocol):
    """Narrow Temporal boundary used by readiness and reconciliation."""

    def is_available(self) -> bool:
        """Return whether Temporal answers its health RPC."""

    def list_task_workflow_statuses(self) -> dict[str, str]:
        """Return the latest known status for each code-agent task workflow."""


class TemporalOperationalProbe:
    """Bounded synchronous facade around the Temporal async client."""

    def __init__(
        self,
        *,
        address: str,
        timeout_seconds: int = TEMPORAL_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.address = address
        self.timeout_seconds = timeout_seconds

    def _run(self, operation: Coroutine[Any, Any, _T]) -> _T:
        bounded: Coroutine[Any, Any, _T] = asyncio.wait_for(operation, timeout=self.timeout_seconds)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(bounded)

        def run_bounded() -> _T:
            return asyncio.run(bounded)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="temporal-probe") as executor:
            return executor.submit(run_bounded).result()

    async def _connect(self) -> Client:
        client = await Client.connect(self.address)
        if not await client.service_client.check_health(timeout=timedelta(seconds=1)):
            raise ConnectionError("Temporal health RPC returned an unhealthy result.")
        return client

    async def _is_available(self) -> bool:
        await self._connect()
        return True

    def is_available(self) -> bool:
        try:
            return self._run(self._is_available())
        except Exception:
            return False

    async def _list_task_workflow_statuses(self) -> dict[str, str]:
        client = await self._connect()
        latest: dict[str, tuple[Any, str]] = {}
        workflows = client.list_workflows("WorkflowType = 'TaskExecutionWorkflow'")
        async for workflow in workflows:
            if not workflow.id.startswith("task-") or workflow.status is None:
                continue
            task_id = workflow.id.removeprefix("task-")
            status = workflow.status.name.lower()
            prior = latest.get(task_id)
            if prior is None or status == "running" or workflow.start_time > prior[0]:
                latest[task_id] = (workflow.start_time, status)
        return {task_id: value[1] for task_id, value in latest.items()}

    def list_task_workflow_statuses(self) -> dict[str, str]:
        return self._run(self._list_task_workflow_statuses())


def _temporal_probe(self: Any) -> TemporalOperationalProbeProtocol:
    configured = getattr(self, "temporal_operational_probe", None)
    if configured is not None:
        return configured
    return TemporalOperationalProbe(address=os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))


def _bounded_task_ids(task_ids: list[str]) -> tuple[list[str], bool]:
    unique_ids = sorted(set(task_ids))
    return unique_ids[:AFFECTED_TASK_ID_LIMIT], len(unique_ids) > AFFECTED_TASK_ID_LIMIT


def _database_snapshots(
    self: Any,
) -> tuple[
    OutboxOperationalSnapshot,
    WorkerOperationalSnapshot,
    InteractionOperationalSnapshot,
    dict[str, TemporalTaskProjection],
]:
    now = utc_now()
    with session_scope(self.session_factory) as session:
        repo = OperationalHealthRepository(session)
        repo.ping(timeout_seconds=POSTGRES_PROBE_TIMEOUT_SECONDS)
        return (
            repo.outbox_snapshot(now=now, task_id_limit=AFFECTED_TASK_ID_LIMIT + 1),
            repo.worker_snapshot(now=now, fresh_seconds=WORKER_FRESH_SECONDS),
            repo.interaction_snapshot(
                now=now,
                stuck_seconds=INTERACTION_STUCK_SECONDS,
                task_id_limit=AFFECTED_TASK_ID_LIMIT + 1,
            ),
            repo.temporal_task_projections(),
        )


def _reconciliation_metrics(
    self: Any,
    *,
    checked_at: Any,
    projections: dict[str, TemporalTaskProjection],
) -> TerminalReconciliationMetrics:
    try:
        workflow_statuses = _temporal_probe(self).list_task_workflow_statuses()
    except Exception as exc:
        logger.warning("Temporal terminal reconciliation probe failed", exc_info=exc)
        return TerminalReconciliationMetrics(status="unknown", checked_at=checked_at)

    grace_cutoff = checked_at - timedelta(seconds=TERMINAL_RECONCILIATION_GRACE_SECONDS)
    divergent: list[str] = []
    for task_id, projection in projections.items():
        workflow_status = workflow_statuses.get(task_id)
        postgres_terminal = projection.status in _TERMINAL_STATUSES
        temporal_running = workflow_status in _TEMPORAL_RUNNING_STATUSES
        if postgres_terminal:
            if projection.task_updated_at <= grace_cutoff and temporal_running:
                divergent.append(task_id)
            continue
        if projection.start_delivered_at > grace_cutoff:
            continue
        if not temporal_running:
            divergent.append(task_id)

    for task_id, workflow_status in workflow_statuses.items():
        if workflow_status in _TEMPORAL_RUNNING_STATUSES and task_id not in projections:
            divergent.append(task_id)

    affected, truncated = _bounded_task_ids(divergent)
    return TerminalReconciliationMetrics(
        status="degraded" if divergent else "ok",
        divergence_count=len(set(divergent)),
        affected_task_ids=affected,
        affected_task_ids_truncated=truncated,
        checked_at=checked_at,
    )


def get_execution_health(self: Any) -> ExecutionHealthMetrics:
    """Return current authenticated execution-health signals."""
    checked_at = utc_now()
    outbox, workers, interactions, projections = _database_snapshots(self)
    outbox_ids, outbox_truncated = _bounded_task_ids(outbox.affected_task_ids)
    interaction_ids, interaction_truncated = _bounded_task_ids(interactions.affected_task_ids)
    reconciliation = _reconciliation_metrics(
        self,
        checked_at=checked_at,
        projections=projections,
    )
    degraded_reasons: list[str] = []
    if outbox.retrying_count:
        degraded_reasons.append("command_retries_present")
    if outbox.dead_letter_count:
        degraded_reasons.append("command_dead_letters_present")
    if (outbox.oldest_eligible_age_seconds or 0) > OUTBOX_STALE_SECONDS:
        degraded_reasons.append("dispatcher_backlog_stale")
    if workers.fresh_count == 0:
        degraded_reasons.append("worker_unavailable")
    if workers.fresh_dispatcher_count == 0:
        degraded_reasons.append("dispatcher_unavailable")
    if interactions.stuck_count:
        degraded_reasons.append("interaction_wait_stuck")
    if reconciliation.status == "degraded":
        degraded_reasons.append("terminal_state_divergence")
    elif reconciliation.status == "unknown":
        degraded_reasons.append("terminal_reconciliation_unknown")

    return ExecutionHealthMetrics(
        outbox=OutboxHealthMetrics(
            pending_count=outbox.pending_count,
            retrying_count=outbox.retrying_count,
            dead_letter_count=outbox.dead_letter_count,
            oldest_unresolved_age_seconds=outbox.oldest_unresolved_age_seconds,
            oldest_eligible_age_seconds=outbox.oldest_eligible_age_seconds,
            affected_task_ids=outbox_ids,
            affected_task_ids_truncated=outbox_truncated,
        ),
        workers=WorkerHealthMetrics(
            fresh_count=workers.fresh_count,
            stale_count=workers.stale_count,
            fresh_dispatcher_count=workers.fresh_dispatcher_count,
            freshest_heartbeat_at=workers.freshest_heartbeat_at,
            freshest_heartbeat_age_seconds=workers.freshest_heartbeat_age_seconds,
            freshest_dispatcher_heartbeat_at=workers.freshest_dispatcher_heartbeat_at,
            freshest_dispatcher_heartbeat_age_seconds=(
                workers.freshest_dispatcher_heartbeat_age_seconds
            ),
        ),
        interactions=InteractionWaitMetrics(
            pending_count=interactions.pending_count,
            stuck_count=interactions.stuck_count,
            oldest_pending_age_seconds=interactions.oldest_pending_age_seconds,
            affected_task_ids=interaction_ids,
            affected_task_ids_truncated=interaction_truncated,
        ),
        reconciliation=reconciliation,
        degraded_reasons=degraded_reasons,
    )


def get_readiness(self: Any) -> ReadinessSnapshot:
    """Return dependency-aware execution readiness without leaking errors."""
    checked_at = utc_now()
    components: dict[str, ReadinessComponent] = {}
    degraded_reasons: list[str] = []
    try:
        outbox, workers, _, _ = _database_snapshots(self)
    except Exception as exc:
        logger.warning("Postgres readiness probe failed", exc_info=exc)
        degraded_reasons.append("postgres_unavailable")
        components["postgres"] = ReadinessComponent(
            status="not_ready", reasons=["postgres_unavailable"]
        )
        components["worker"] = ReadinessComponent(
            status="unknown", reasons=["postgres_unavailable"]
        )
        components["dispatcher"] = ReadinessComponent(
            status="unknown", reasons=["postgres_unavailable"]
        )
    else:
        components["postgres"] = ReadinessComponent(status="ready")
        worker_reasons = [] if workers.fresh_count else ["worker_unavailable"]
        components["worker"] = ReadinessComponent(
            status="ready" if not worker_reasons else "not_ready",
            reasons=worker_reasons,
            last_observed_at=workers.freshest_heartbeat_at,
        )
        dispatcher_reasons: list[str] = []
        if workers.fresh_dispatcher_count == 0:
            dispatcher_reasons.append("dispatcher_unavailable")
        if (outbox.oldest_eligible_age_seconds or 0) > OUTBOX_STALE_SECONDS:
            dispatcher_reasons.append("dispatcher_backlog_stale")
        components["dispatcher"] = ReadinessComponent(
            status="ready" if not dispatcher_reasons else "not_ready",
            reasons=dispatcher_reasons,
            last_observed_at=workers.freshest_dispatcher_heartbeat_at,
        )
        degraded_reasons.extend(worker_reasons)
        degraded_reasons.extend(dispatcher_reasons)

    if _temporal_probe(self).is_available():
        components["temporal"] = ReadinessComponent(status="ready")
    else:
        components["temporal"] = ReadinessComponent(
            status="not_ready", reasons=["temporal_unavailable"]
        )
        degraded_reasons.append("temporal_unavailable")

    component_order = ("postgres", "temporal", "worker", "dispatcher")
    ordered_components = {name: components[name] for name in component_order}
    unique_reasons = list(dict.fromkeys(degraded_reasons))
    ready = all(component.status == "ready" for component in ordered_components.values())
    return ReadinessSnapshot(
        status="ready" if ready else "not_ready",
        checked_at=checked_at,
        components=ordered_components,
        degraded_reasons=unique_reasons,
    )


def ensure_temporal_available(self: Any) -> None:
    """Preserve bounded submission-time Temporal availability enforcement."""
    for attempt in range(3):
        if _temporal_probe(self).is_available():
            return
        if attempt < 2:
            import time

            time.sleep(0.1 * (2**attempt))
    from orchestrator.execution import TemporalUnavailableError

    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    raise TemporalUnavailableError(
        f"Temporal is unavailable at {address}; new tasks are temporarily disabled."
    )
