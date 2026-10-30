"""Persistence operations for durable readiness alert transitions and delivery."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, aliased

from db.base import generate_uuid, utc_now
from db.readiness_alert_models import ReadinessAlertDelivery, ReadinessAlertState
from orchestrator.operational_health_types import READINESS_REASON_CODES

MAX_DELIVERY_ATTEMPTS = 8
DELIVERY_LEASE_SECONDS = 30
INTERACTION_REASON_PREFIX = "interaction_wait_stuck|"
OUTBOX_STALE_REASON_PREFIX = "outbox_command_stale|"
TERMINAL_RECONCILIATION_REASON_PREFIX = "terminal_reconciliation_divergent|"
TASK_REASON_PREFIXES = (
    INTERACTION_REASON_PREFIX,
    OUTBOX_STALE_REASON_PREFIX,
    TERMINAL_RECONCILIATION_REASON_PREFIX,
)


def _utc_aware(value: datetime) -> datetime:
    """Normalize SQLite's naive datetime values for observation ordering."""
    return value.replace(tzinfo=value.tzinfo or utc_now().tzinfo)


class ReadinessAlertRepository:
    """Record readiness transitions and coordinate bounded delivery retries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _lock_state_updates(self) -> None:
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            self.session.execute(text("SELECT pg_advisory_xact_lock(731004219)"))

    def capture_observed_states(self) -> dict[str, tuple[bool, datetime, datetime, int]]:
        """Capture latest known component states before replaying buffered samples."""
        self._lock_state_updates()
        return {
            state.reason: (
                state.degraded,
                state.last_checked_at,
                state.last_transition_at,
                state.transition_number,
            )
            for state in self.session.scalars(select(ReadinessAlertState)).all()
        }

    def record_earlier_incidents(
        self,
        samples: list[tuple[list[str], list[str], datetime]],
        *,
        captured: dict[str, tuple[bool, datetime, datetime, int]],
    ) -> None:
        """Retain completed buffered incidents in gaps before recorded transitions."""
        states = {
            state.reason: state
            for state in self.session.scalars(select(ReadinessAlertState).with_for_update()).all()
            if not state.reason.startswith(TASK_REASON_PREFIXES)
        }
        for reason, state in states.items():
            history = self.session.scalars(
                select(ReadinessAlertDelivery)
                .where(ReadinessAlertDelivery.reason == reason)
                .order_by(ReadinessAlertDelivery.observed_at)
            ).all()
            if not history:
                baseline = captured.get(reason)
                if baseline is not None and not baseline[0] and baseline[3] == 0:
                    self._record_before_initial_baseline(
                        reason, state, samples, _utc_aware(baseline[1])
                    )
                continue
            events = [(_utc_aware(item.observed_at), item.transition) for item in history]
            started: datetime | None = None
            for reasons, unknown, observed_at in samples:
                if reason in unknown:
                    continue
                observed_at = _utc_aware(observed_at)
                if reason in reasons:
                    if started is None:
                        started = observed_at
                    continue
                if started is None:
                    continue
                previous = [event for event in events if event[0] <= started]
                between = [event for event in events if started < event[0] <= observed_at]
                has_later = any(event[0] > observed_at for event in events)
                if has_later and not between and (not previous or previous[-1][1] == "recovered"):
                    state.transition_number += 1
                    self._enqueue(reason, state.transition_number, "degraded", started)
                    state.transition_number += 1
                    self._enqueue(reason, state.transition_number, "recovered", observed_at)
                    events.extend([(started, "degraded"), (observed_at, "recovered")])
                    events.sort()
                started = None

    def _record_before_initial_baseline(
        self,
        reason: str,
        state: ReadinessAlertState,
        samples: list[tuple[list[str], list[str], datetime]],
        baseline_at: datetime,
    ) -> None:
        """Replay an outage closed by an initial healthy observation."""
        if state.degraded or state.transition_number != 0:
            return
        started: datetime | None = None
        saw_unknown_after_start = False
        for reasons, unknown, observed_at in samples:
            observed_at = _utc_aware(observed_at)
            if reason in unknown:
                if started is not None and observed_at > started:
                    saw_unknown_after_start = True
                continue
            if reason in reasons:
                if started is None:
                    started = observed_at
                continue
            if started is not None and started < observed_at <= baseline_at:
                self._enqueue_historical_incident(state, reason, started, observed_at)
            elif started is not None and saw_unknown_after_start and started < baseline_at:
                self._enqueue_historical_incident(state, reason, started, baseline_at)
            started = None
            saw_unknown_after_start = False
        if started is not None and saw_unknown_after_start and started < baseline_at:
            self._enqueue_historical_incident(state, reason, started, baseline_at)

    def _enqueue_historical_incident(
        self, state: ReadinessAlertState, reason: str, started: datetime, recovered: datetime
    ) -> None:
        state.transition_number += 1
        self._enqueue(reason, state.transition_number, "degraded", started)
        state.transition_number += 1
        self._enqueue(reason, state.transition_number, "recovered", recovered)

    def restore_newer_observations(
        self,
        captured: dict[str, tuple[bool, datetime, datetime, int]],
        latest_replayed_at: dict[str, datetime],
    ) -> None:
        """Keep a newer known component state after replaying older buffered transitions."""
        states = {
            state.reason: state
            for state in self.session.scalars(select(ReadinessAlertState).with_for_update()).all()
            if not state.reason.startswith(TASK_REASON_PREFIXES)
        }
        for reason, replayed_at in latest_replayed_at.items():
            baseline = captured.get(reason)
            state = states.get(reason)
            if baseline is None or state is None:
                continue
            degraded, checked_at, transition_at, transition_number = baseline
            if _utc_aware(checked_at) < _utc_aware(replayed_at):
                continue
            if state.degraded != degraded:
                state.degraded = degraded
                state.transition_number += 1
                state.last_transition_at = checked_at
                self._enqueue(
                    reason,
                    state.transition_number,
                    "degraded" if degraded else "recovered",
                    checked_at,
                )
            elif _utc_aware(state.last_transition_at) < _utc_aware(transition_at):
                state.last_transition_at = transition_at
            state.last_checked_at = max((_utc_aware(state.last_checked_at), _utc_aware(checked_at)))
            state.transition_number = max(state.transition_number, transition_number)

    def record_snapshot(
        self,
        degraded_reasons: list[str],
        *,
        unknown_reasons: list[str] | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        """Persist ordered transitions, ignoring stale and currently unknown observations."""
        self._lock_state_updates()
        now = observed_at or utc_now()
        active = set(degraded_reasons)
        unknown = set(unknown_reasons or ())
        states = {
            state.reason: state
            for state in self.session.scalars(select(ReadinessAlertState).with_for_update()).all()
            if not state.reason.startswith(TASK_REASON_PREFIXES)
        }
        observed_reasons = active | set(READINESS_REASON_CODES)
        observed_reasons.update(states)
        for reason in sorted(observed_reasons):
            if reason in unknown:
                continue
            state = states.get(reason)
            if state is not None and _utc_aware(state.last_checked_at) >= _utc_aware(now):
                self._record_stale_transition(
                    state,
                    reason,
                    degraded=reason in active,
                    observed_at=now,
                )
                continue
            degraded = reason in active
            if state is None:
                state = ReadinessAlertState(
                    reason=reason,
                    degraded=degraded,
                    transition_number=1 if degraded else 0,
                    last_transition_at=now,
                    last_checked_at=now,
                )
                self.session.add(state)
                if degraded:
                    self._enqueue(reason, 1, "degraded", now)
            elif state.degraded != degraded:
                state.degraded = degraded
                state.transition_number += 1
                state.last_transition_at = now
                state.last_checked_at = now
                self._enqueue(
                    reason,
                    state.transition_number,
                    "degraded" if degraded else "recovered",
                    now,
                )
            else:
                state.last_checked_at = now

    def record_interaction_snapshot(
        self, task_ids: list[str], *, observed_at: datetime | None = None
    ) -> None:
        """Persist per-task transitions from one complete, successful observation."""
        self._record_task_snapshot(
            task_ids, observed_at=observed_at, reason_prefix=INTERACTION_REASON_PREFIX
        )

    def record_stale_outbox_snapshot(
        self, task_ids: list[str], *, observed_at: datetime | None = None
    ) -> None:
        """Persist stale-outbox transitions from one complete, successful observation."""
        self._record_task_snapshot(
            task_ids, observed_at=observed_at, reason_prefix=OUTBOX_STALE_REASON_PREFIX
        )

    def record_terminal_reconciliation_snapshot(
        self, task_ids: list[str], *, observed_at: datetime | None = None
    ) -> None:
        """Persist per-task reconciliation transitions from a complete observation."""
        self._record_task_snapshot(
            task_ids,
            observed_at=observed_at,
            reason_prefix=TERMINAL_RECONCILIATION_REASON_PREFIX,
        )

    def _record_task_snapshot(
        self,
        task_ids: list[str],
        *,
        observed_at: datetime | None,
        reason_prefix: str,
    ) -> None:
        """Persist one namespaced complete-set observation with its own watermark."""
        self._lock_state_updates()
        now = observed_at or utc_now()
        current = {f"{reason_prefix}{task_id}" for task_id in task_ids}
        watermark_key = f"{reason_prefix}__observation_watermark__"
        states = {
            state.reason: state
            for state in self.session.scalars(
                select(ReadinessAlertState)
                .where(ReadinessAlertState.reason.like(f"{reason_prefix}%"))
                .with_for_update()
            ).all()
        }
        watermark = states.get(watermark_key)
        if watermark is not None and _utc_aware(watermark.last_checked_at) >= _utc_aware(now):
            return
        if watermark is None:
            watermark = ReadinessAlertState(
                reason=watermark_key,
                degraded=False,
                transition_number=0,
                last_transition_at=now,
                last_checked_at=now,
            )
            self.session.add(watermark)
        else:
            watermark.last_checked_at = now
        states.pop(watermark_key, None)
        for reason in sorted(current | set(states)):
            state = states.get(reason)
            if state is None:
                state = ReadinessAlertState(
                    reason=reason,
                    degraded=True,
                    transition_number=1,
                    last_transition_at=now,
                    last_checked_at=now,
                )
                self.session.add(state)
                self._enqueue(reason, 1, "degraded", now)
                continue
            if _utc_aware(state.last_checked_at) >= _utc_aware(now):
                continue
            degraded = reason in current
            state.last_checked_at = now
            if state.degraded != degraded:
                state.degraded = degraded
                state.transition_number += 1
                state.last_transition_at = now
                self._enqueue(
                    reason,
                    state.transition_number,
                    "degraded" if degraded else "recovered",
                    now,
                )
            else:
                state.last_checked_at = now

    def _record_stale_transition(
        self,
        state: ReadinessAlertState,
        reason: str,
        *,
        degraded: bool,
        observed_at: datetime,
    ) -> None:
        """Replay an unrecorded transition without moving the latest-sample watermark."""
        if state.degraded == degraded:
            return
        after_latest_transition = _utc_aware(observed_at) > _utc_aware(state.last_transition_at)
        if not after_latest_transition:
            return
        state.degraded = degraded
        state.transition_number += 1
        state.last_transition_at = observed_at
        self._enqueue(
            reason,
            state.transition_number,
            "degraded" if degraded else "recovered",
            observed_at,
        )

    def _enqueue(self, reason: str, number: int, transition: str, now: Any) -> None:
        self.session.add(
            ReadinessAlertDelivery(
                id=generate_uuid(),
                reason=reason,
                transition_number=number,
                transition=transition,
                observed_at=now,
                status="pending",
                attempts=0,
                next_attempt_at=now,
            )
        )

    def claim_due(self, *, limit: int = 20) -> list[ReadinessAlertDelivery]:
        """Lease a bounded batch of due notifications for one API instance."""
        now = utc_now()
        earlier_unfinished = aliased(ReadinessAlertDelivery)
        earlier_transition_exists = (
            select(earlier_unfinished.id)
            .where(
                earlier_unfinished.reason == ReadinessAlertDelivery.reason,
                earlier_unfinished.transition_number < ReadinessAlertDelivery.transition_number,
                earlier_unfinished.status.in_(("pending", "processing")),
            )
            .exists()
        )
        due = self.session.scalars(
            select(ReadinessAlertDelivery)
            .where(
                or_(
                    (ReadinessAlertDelivery.status == "pending")
                    & (ReadinessAlertDelivery.next_attempt_at <= now),
                    (ReadinessAlertDelivery.status == "processing")
                    & (ReadinessAlertDelivery.lease_expires_at <= now),
                ),
                ~earlier_transition_exists,
            )
            .order_by(ReadinessAlertDelivery.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for delivery in due:
            delivery.status = "processing"
            delivery.attempts += 1
            delivery.lease_expires_at = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
        return list(due)

    def mark_delivered(self, delivery_id: str, *, http_status: int, now: Any = None) -> None:
        delivery = self.session.get(ReadinessAlertDelivery, delivery_id)
        if delivery is None:
            return
        delivery.status = "delivered"
        delivery.delivered_at = now or utc_now()
        delivery.lease_expires_at = None
        delivery.last_failure_code = None
        delivery.last_http_status = http_status

    def mark_failed(
        self, delivery_id: str, *, http_status: int | None = None, now: Any = None
    ) -> None:
        delivery = self.session.get(ReadinessAlertDelivery, delivery_id)
        if delivery is None:
            return
        checked_at = now or utc_now()
        delivery.last_failure_code = "telegram_request_failed"
        delivery.last_http_status = http_status
        delivery.lease_expires_at = None
        if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
            delivery.status = "failed"
            return
        delivery.status = "pending"
        delay = min(5 * (2 ** (delivery.attempts - 1)), 900)
        delivery.next_attempt_at = checked_at + timedelta(seconds=delay)

    def list_states(self) -> list[ReadinessAlertState]:
        """Return reasons that have produced at least one alert transition."""
        return list(
            self.session.scalars(
                select(ReadinessAlertState)
                .where(ReadinessAlertState.transition_number > 0)
                .order_by(ReadinessAlertState.reason)
            )
        )

    def list_deliveries(self, *, limit: int = 100) -> list[ReadinessAlertDelivery]:
        """Return newest delivery records, including failures for inspection."""
        return list(
            self.session.scalars(
                select(ReadinessAlertDelivery)
                .order_by(
                    ReadinessAlertDelivery.created_at.desc(),
                    ReadinessAlertDelivery.transition_number.desc(),
                    ReadinessAlertDelivery.id.desc(),
                )
                .limit(limit)
            )
        )
