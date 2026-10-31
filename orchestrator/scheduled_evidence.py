"""Collect bounded, sanitized evidence for scheduled read-only summaries."""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from db.base import utc_now
from orchestrator.operational_health_types import READINESS_REASON_CODES
from repositories import session_scope
from repositories.operational_evidence import SOURCES, latest_observation, read_source

logger = logging.getLogger(__name__)
LIMIT = 64 * 1024
REASONS = frozenset(
    (
        *READINESS_REASON_CODES,
        "interaction_wait_stuck",
        "outbox_command_stale",
        "terminal_reconciliation_divergent",
        "provider_capability",
    )
)


def _time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    ).isoformat()


def _reason(value: str) -> str:
    prefix = value.split("|", 1)[0]
    if prefix == "provider":
        prefix = "provider_capability"
    return prefix if prefix in REASONS else "other"


def _record(row: Any, name: str) -> dict[str, Any]:
    data = {
        "reference": row.id,
        "reason_category": _reason(row.reason),
        "transition_number": row.transition_number,
    }
    if name == "acknowledgments":
        data["acknowledged_at"] = _time(row.acknowledged_at)
    else:
        data.update(
            {
                "observed_at": _time(row.observed_at),
                "transition": row.transition
                if row.transition in ("degraded", "recovered")
                else "other",
                "status": row.status
                if row.status in ("pending", "processing", "delivered", "failed", "suppressed")
                else "other",
                "attempts": row.attempts,
                "delivered_at": _time(row.delivered_at),
            }
        )
    return data


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode())


def collect_evidence(service: Any, cutoff: datetime, lookback_hours: int) -> dict[str, Any]:
    start = cutoff - timedelta(hours=lookback_hours)
    payload: dict[str, Any] = {
        "version": 1,
        "source": "operational_incidents",
        "interval": {"start": _time(start), "end": _time(cutoff)},
        "collected_at": _time(utc_now()),
        "latest_observation_at": None,
        "sources": {},
        "truncated": False,
        "limitations": [
            "Service-wide evidence; repository selects only the execution workspace.",
            "Channel-dependent delivery history is not a complete incident ledger; "
            "retention and channel configuration limit coverage.",
            "Current delivery status may have changed after the interval.",
            "Concurrent changes and overwritten monitoring state may limit the snapshot.",
        ],
    }
    try:
        with session_scope(service.session_factory) as session:
            payload["latest_observation_at"] = _time(latest_observation(session, cutoff))
    except Exception:
        logger.warning("Scheduled evidence observation timestamp unavailable")
        payload["limitations"].append("Latest monitoring observation unavailable.")
    for name in SOURCES:
        try:
            with session_scope(service.session_factory) as session:
                rows, total = read_source(session, name, start, cutoff)
                records = [_record(row, name) for row in rows]
            omitted = max(total - len(records), 0)
            payload["sources"][name] = {
                "status": "truncated" if omitted else "observed" if total else "empty",
                "records": records,
                "omitted_count": omitted,
            }
        except Exception:
            logger.warning("Scheduled evidence source unavailable: %s", name)
            payload["sources"][name] = {
                "status": "unavailable",
                "records": [],
                "omitted_count": None,
            }
    for source in payload["sources"].values():
        while _size(payload) > LIMIT and source["records"]:
            source["records"].pop()
            source["omitted_count"] += 1
            source["status"] = "truncated"
    payload["truncated"] = any(
        source["status"] == "truncated" for source in payload["sources"].values()
    )
    return payload
