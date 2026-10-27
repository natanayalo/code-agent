"""Persistence operations for durable readiness alert transitions and delivery."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from db.base import generate_uuid, utc_now
from db.readiness_alert_models import ReadinessAlertDelivery, ReadinessAlertState

MAX_DELIVERY_ATTEMPTS = 8
DELIVERY_LEASE_SECONDS = 30


class ReadinessAlertRepository:
    """Record readiness transitions and coordinate bounded delivery retries."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record_snapshot(self, degraded_reasons: list[str]) -> None:
        """Persist reason transitions and enqueue one delivery per transition."""
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            self.session.execute(text("SELECT pg_advisory_xact_lock(731004219)"))
        now = utc_now()
        active = set(degraded_reasons)
        states = {
            state.reason: state
            for state in self.session.scalars(select(ReadinessAlertState).with_for_update()).all()
        }
        for reason in active | {key for key, state in states.items() if state.degraded}:
            state = states.get(reason)
            degraded = reason in active
            if state is None:
                if not degraded:
                    continue
                state = ReadinessAlertState(
                    reason=reason,
                    degraded=True,
                    transition_number=1,
                    last_transition_at=now,
                    last_checked_at=now,
                )
                self.session.add(state)
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

    def _enqueue(self, reason: str, number: int, transition: str, now: Any) -> None:
        self.session.add(
            ReadinessAlertDelivery(
                id=generate_uuid(),
                reason=reason,
                transition_number=number,
                transition=transition,
                status="pending",
                attempts=0,
                next_attempt_at=now,
            )
        )

    def claim_due(self, *, limit: int = 20) -> list[ReadinessAlertDelivery]:
        """Lease a bounded batch of due notifications for one API instance."""
        now = utc_now()
        due = self.session.scalars(
            select(ReadinessAlertDelivery)
            .where(
                or_(
                    (ReadinessAlertDelivery.status == "pending")
                    & (ReadinessAlertDelivery.next_attempt_at <= now),
                    (ReadinessAlertDelivery.status == "processing")
                    & (ReadinessAlertDelivery.lease_expires_at <= now),
                )
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
        """Return current persisted reason states in stable order."""
        return list(
            self.session.scalars(select(ReadinessAlertState).order_by(ReadinessAlertState.reason))
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
