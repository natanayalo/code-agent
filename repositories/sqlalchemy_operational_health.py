"""Read-only persistence queries for execution health and readiness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from db.enums import HumanInteractionStatus, OrchestrationRuntime, TaskStatus, WorkerNodeStatus
from db.models import HumanInteraction, Task, TemporalCommand, WorkerNode

_TERMINAL_TASK_STATUSES = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _age_seconds(now: datetime, value: datetime | None) -> float | None:
    aware_value = _as_aware(value)
    if aware_value is None:
        return None
    return max(0.0, (now - aware_value).total_seconds())


@dataclass(frozen=True)
class OutboxOperationalSnapshot:
    pending_count: int
    retrying_count: int
    dead_letter_count: int
    oldest_unresolved_age_seconds: float | None
    oldest_eligible_age_seconds: float | None
    affected_task_ids: list[str]


@dataclass(frozen=True)
class WorkerOperationalSnapshot:
    fresh_count: int
    stale_count: int
    fresh_dispatcher_count: int
    freshest_heartbeat_at: datetime | None
    freshest_heartbeat_age_seconds: float | None
    freshest_dispatcher_heartbeat_at: datetime | None
    freshest_dispatcher_heartbeat_age_seconds: float | None


@dataclass(frozen=True)
class InteractionOperationalSnapshot:
    pending_count: int
    stuck_count: int
    oldest_pending_age_seconds: float | None
    affected_task_ids: list[str]


@dataclass(frozen=True)
class TemporalTaskProjection:
    status: str
    start_delivered_at: datetime
    task_updated_at: datetime


class OperationalHealthRepository:
    """Aggregate current operational state without applying readiness policy."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def ping(self, *, timeout_seconds: int) -> None:
        """Prove the configured database can execute a query."""
        bind = self.session.get_bind()
        if bind.dialect.name == "postgresql":
            timeout_ms = str(max(1, timeout_seconds * 1000))
            self.session.scalar(select(func.set_config("statement_timeout", timeout_ms, True)))
        self.session.execute(select(1)).scalar_one()

    def outbox_snapshot(self, *, now: datetime, task_id_limit: int) -> OutboxOperationalSnapshot:
        unresolved = (
            TemporalCommand.delivered_at.is_(None),
            TemporalCommand.dead_lettered_at.is_(None),
            TemporalCommand.superseded_at.is_(None),
        )
        pending_count = self.session.scalar(
            select(func.count(TemporalCommand.id)).where(*unresolved, TemporalCommand.attempts == 0)
        )
        retrying_count = self.session.scalar(
            select(func.count(TemporalCommand.id)).where(*unresolved, TemporalCommand.attempts > 0)
        )
        dead_letter_count = self.session.scalar(
            select(func.count(TemporalCommand.id)).where(
                TemporalCommand.dead_lettered_at.is_not(None),
                TemporalCommand.delivered_at.is_(None),
                TemporalCommand.superseded_at.is_(None),
            )
        )
        oldest_unresolved_at = self.session.scalar(
            select(func.min(TemporalCommand.created_at)).where(*unresolved)
        )
        eligible = (
            *unresolved,
            TemporalCommand.next_attempt_at <= now,
            or_(
                TemporalCommand.claim_token.is_(None),
                TemporalCommand.claim_expires_at.is_(None),
                TemporalCommand.claim_expires_at <= now,
            ),
        )
        oldest_eligible_at = self.session.scalar(
            select(func.min(TemporalCommand.created_at)).where(*eligible)
        )
        affected = list(
            self.session.scalars(
                select(TemporalCommand.task_id)
                .where(
                    TemporalCommand.delivered_at.is_(None),
                    TemporalCommand.superseded_at.is_(None),
                )
                .distinct()
                .order_by(TemporalCommand.task_id)
                .limit(task_id_limit)
            )
        )
        return OutboxOperationalSnapshot(
            pending_count=int(pending_count or 0),
            retrying_count=int(retrying_count or 0),
            dead_letter_count=int(dead_letter_count or 0),
            oldest_unresolved_age_seconds=_age_seconds(now, oldest_unresolved_at),
            oldest_eligible_age_seconds=_age_seconds(now, oldest_eligible_at),
            affected_task_ids=affected,
        )

    def worker_snapshot(self, *, now: datetime, fresh_seconds: int) -> WorkerOperationalSnapshot:
        nodes = list(self.session.scalars(select(WorkerNode)))
        cutoff = now - timedelta(seconds=fresh_seconds)
        fresh_nodes = [
            node
            for node in nodes
            if node.status == WorkerNodeStatus.ACTIVE
            and (_as_aware(node.last_heartbeat_at) or now) >= cutoff
        ]
        stale_nodes = [
            node for node in nodes if (_as_aware(node.last_heartbeat_at) or now) < cutoff
        ]
        dispatcher_nodes = [
            node
            for node in nodes
            if isinstance(node.capabilities, dict)
            and node.capabilities.get("command_dispatcher") is True
        ]
        fresh_dispatchers = [
            node
            for node in fresh_nodes
            if isinstance(node.capabilities, dict)
            and node.capabilities.get("command_dispatcher") is True
        ]
        heartbeat_times = [
            heartbeat
            for node in nodes
            if (heartbeat := _as_aware(node.last_heartbeat_at)) is not None
        ]
        freshest = max(heartbeat_times, default=None)
        dispatcher_heartbeat_times = [
            heartbeat
            for node in dispatcher_nodes
            if (heartbeat := _as_aware(node.last_heartbeat_at)) is not None
        ]
        freshest_dispatcher = max(dispatcher_heartbeat_times, default=None)
        return WorkerOperationalSnapshot(
            fresh_count=len(fresh_nodes),
            stale_count=len(stale_nodes),
            fresh_dispatcher_count=len(fresh_dispatchers),
            freshest_heartbeat_at=freshest,
            freshest_heartbeat_age_seconds=_age_seconds(now, freshest),
            freshest_dispatcher_heartbeat_at=freshest_dispatcher,
            freshest_dispatcher_heartbeat_age_seconds=_age_seconds(now, freshest_dispatcher),
        )

    def interaction_snapshot(
        self, *, now: datetime, stuck_seconds: int, task_id_limit: int
    ) -> InteractionOperationalSnapshot:
        pending_rows = list(
            self.session.execute(
                select(HumanInteraction.task_id, HumanInteraction.created_at)
                .join(Task, Task.id == HumanInteraction.task_id)
                .where(
                    HumanInteraction.status == HumanInteractionStatus.PENDING,
                    Task.status.not_in(_TERMINAL_TASK_STATUSES),
                )
                .order_by(HumanInteraction.created_at.asc())
            )
        )
        stuck_cutoff = now - timedelta(seconds=stuck_seconds)
        stuck_rows = [
            row for row in pending_rows if (_as_aware(row.created_at) or now) < stuck_cutoff
        ]
        affected = sorted({row.task_id for row in stuck_rows})[:task_id_limit]
        oldest_pending_at = _as_aware(pending_rows[0].created_at) if pending_rows else None
        return InteractionOperationalSnapshot(
            pending_count=len(pending_rows),
            stuck_count=len(stuck_rows),
            oldest_pending_age_seconds=_age_seconds(now, oldest_pending_at),
            affected_task_ids=affected,
        )

    def temporal_task_projections(self) -> dict[str, TemporalTaskProjection]:
        """Return Temporal task projections whose start command reached Temporal."""
        rows = self.session.execute(
            select(Task.id, Task.status, Task.updated_at, TemporalCommand.delivered_at)
            .join(TemporalCommand, TemporalCommand.task_id == Task.id)
            .where(
                Task.orchestration_runtime == OrchestrationRuntime.TEMPORAL,
                TemporalCommand.command_type == "start",
                TemporalCommand.delivered_at.is_not(None),
            )
            .distinct()
        )
        return {
            task_id: TemporalTaskProjection(
                status=status.value if hasattr(status, "value") else str(status),
                start_delivered_at=_as_aware(delivered_at) or delivered_at,
                task_updated_at=_as_aware(updated_at) or updated_at,
            )
            for task_id, status, updated_at, delivered_at in rows
            if delivered_at is not None
        }
