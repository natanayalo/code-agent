"""Public readiness and execution-health payloads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OperationalHealthModel(BaseModel):
    """Strict base model for operator-facing health contracts."""

    model_config = ConfigDict(extra="forbid")


class ReadinessComponent(OperationalHealthModel):
    """One dependency result included in the public readiness response."""

    status: Literal["ready", "not_ready", "unknown"]
    reasons: list[str] = Field(default_factory=list)
    last_observed_at: datetime | None = None


class ReadinessSnapshot(OperationalHealthModel):
    """Machine-readable execution-readiness result."""

    status: Literal["ready", "not_ready"]
    checked_at: datetime
    components: dict[str, ReadinessComponent]
    degraded_reasons: list[str] = Field(default_factory=list)


class OutboxHealthMetrics(OperationalHealthModel):
    """Current transactional Temporal-command backlog."""

    pending_count: int
    retrying_count: int
    dead_letter_count: int
    oldest_unresolved_age_seconds: float | None = None
    oldest_eligible_age_seconds: float | None = None
    affected_task_ids: list[str] = Field(default_factory=list)
    affected_task_ids_truncated: bool = False


class WorkerHealthMetrics(OperationalHealthModel):
    """Current worker and dispatcher heartbeat state."""

    fresh_count: int
    stale_count: int
    fresh_dispatcher_count: int
    freshest_heartbeat_at: datetime | None = None
    freshest_heartbeat_age_seconds: float | None = None
    freshest_dispatcher_heartbeat_at: datetime | None = None
    freshest_dispatcher_heartbeat_age_seconds: float | None = None


class InteractionWaitMetrics(OperationalHealthModel):
    """Pending operator interactions and aged waits."""

    pending_count: int
    stuck_count: int
    oldest_pending_age_seconds: float | None = None
    affected_task_ids: list[str] = Field(default_factory=list)
    affected_task_ids_truncated: bool = False


class TerminalReconciliationMetrics(OperationalHealthModel):
    """Comparison between Temporal workflow and Postgres task terminal state."""

    status: Literal["ok", "degraded", "unknown"]
    divergence_count: int | None = None
    affected_task_ids: list[str] = Field(default_factory=list)
    affected_task_ids_truncated: bool = False
    checked_at: datetime


class ExecutionHealthMetrics(OperationalHealthModel):
    """Authenticated operational signals used by metrics and the dashboard."""

    outbox: OutboxHealthMetrics
    workers: WorkerHealthMetrics
    interactions: InteractionWaitMetrics
    reconciliation: TerminalReconciliationMetrics
    degraded_reasons: list[str] = Field(default_factory=list)
