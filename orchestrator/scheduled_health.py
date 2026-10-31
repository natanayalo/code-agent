"""Bounded, sanitized observations for scheduled health-summary tasks."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select

from db.base import utc_now
from db.readiness_alert_models import ReadinessAlertState
from orchestrator.operational_health_types import READINESS_REASON_CODES
from repositories import session_scope

logger = logging.getLogger(__name__)
_EXECUTION_REASONS = frozenset(
    (
        *READINESS_REASON_CODES,
        "command_retries_present",
        "command_dead_letters_present",
        "interaction_wait_stuck",
        "terminal_state_divergence",
        "terminal_reconciliation_unknown",
    )
)


def _readiness(service: Any) -> dict[str, Any]:
    snapshot = service.get_readiness()
    components = {}
    for name in ("postgres", "temporal", "worker", "dispatcher"):
        item = snapshot.components.get(name)
        components[name] = (
            {
                "status": item.status,
                "last_observed_at": item.last_observed_at.isoformat()
                if item.last_observed_at
                else None,
            }
            if item
            else {"status": "unknown", "last_observed_at": None}
        )
    return {
        "status": snapshot.status,
        "checked_at": snapshot.checked_at.isoformat(),
        "components": components,
        "degraded_reasons": [r for r in snapshot.degraded_reasons if r in READINESS_REASON_CODES],
    }


def _execution(service: Any) -> dict[str, Any]:
    metrics = service.get_execution_health()
    return {
        "checked_at": utc_now().isoformat(),
        "outbox": metrics.outbox.model_dump(
            include={
                "pending_count",
                "retrying_count",
                "dead_letter_count",
                "oldest_unresolved_age_seconds",
                "oldest_eligible_age_seconds",
            }
        ),
        "workers": metrics.workers.model_dump(
            mode="json",
            include={
                "fresh_count",
                "stale_count",
                "fresh_dispatcher_count",
                "freshest_heartbeat_at",
                "freshest_heartbeat_age_seconds",
                "freshest_dispatcher_heartbeat_at",
                "freshest_dispatcher_heartbeat_age_seconds",
            },
        ),
        "interactions": metrics.interactions.model_dump(
            include={"pending_count", "stuck_count", "oldest_pending_age_seconds"}
        ),
        "reconciliation": metrics.reconciliation.model_dump(
            mode="json", include={"status", "divergence_count", "checked_at"}
        ),
        "degraded_reasons": [r for r in metrics.degraded_reasons if r in _EXECUTION_REASONS],
    }


def _incidents(service: Any) -> dict[str, Any]:
    with session_scope(service.session_factory) as session:
        active, observed, latest = session.execute(
            select(
                func.count().filter(ReadinessAlertState.degraded.is_(True)),
                func.count(),
                func.max(ReadinessAlertState.last_checked_at),
            ).where(ReadinessAlertState.transition_number > 0)
        ).one()
    return {
        "status": "observed" if observed else "unknown",
        "active_count": active if observed else None,
        "latest_observation_at": latest.isoformat() if latest else None,
    }


def collect_health_snapshot(service: Any) -> dict[str, Any]:
    """Collect each source independently; failures expose no exception details."""
    result: dict[str, Any] = {"collected_at": utc_now().isoformat()}
    for name, collect in (
        ("readiness", _readiness),
        ("execution_health", _execution),
        ("incidents", _incidents),
    ):
        try:
            result[name] = collect(service)
        except Exception:
            logger.warning("Scheduled health observation unavailable: %s", name)
            result[name] = {"status": "unavailable"}
    return result
