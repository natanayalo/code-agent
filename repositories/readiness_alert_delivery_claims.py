"""Lease due Telegram readiness deliveries in transition order."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, aliased

from db.base import utc_now
from db.readiness_alert_models import ReadinessAlertAcknowledgment, ReadinessAlertDelivery

logger = logging.getLogger(__name__)


def claim_due_readiness_deliveries(
    session: Session, *, limit: int, lease_seconds: int, max_attempts: int
) -> list[ReadinessAlertDelivery]:
    """Suppress expired acknowledged incidents, then lease the next due deliveries."""
    now = utc_now()
    acknowledged_incident = (
        select(ReadinessAlertAcknowledgment.id)
        .where(
            ReadinessAlertAcknowledgment.reason == ReadinessAlertDelivery.reason,
            ReadinessAlertAcknowledgment.transition_number
            == ReadinessAlertDelivery.transition_number,
        )
        .exists()
    )
    session.execute(
        update(ReadinessAlertDelivery)
        .where(
            ReadinessAlertDelivery.transition == "degraded",
            ReadinessAlertDelivery.status == "processing",
            ReadinessAlertDelivery.lease_expires_at <= now,
            acknowledged_incident,
        )
        .values(status="suppressed", lease_expires_at=None)
    )
    _fail_exhausted_deliveries(session, now=now, max_attempts=max_attempts)
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
    due = session.scalars(
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
        delivery.lease_expires_at = now + timedelta(seconds=lease_seconds)
    return list(due)


def _fail_exhausted_deliveries(session: Session, *, now: datetime, max_attempts: int) -> None:
    """Finish spent deliveries before checking per-reason transition ordering."""
    exhausted = session.execute(
        update(ReadinessAlertDelivery)
        .where(
            ReadinessAlertDelivery.attempts >= max_attempts,
            or_(
                ReadinessAlertDelivery.status == "pending",
                (ReadinessAlertDelivery.status == "processing")
                & (ReadinessAlertDelivery.lease_expires_at <= now),
            ),
        )
        .values(
            status="failed",
            lease_expires_at=None,
            last_failure_code="telegram_attempts_exhausted",
        )
    )
    if exhausted.rowcount:
        logger.warning(
            "Readiness Telegram delivery retry budget exhausted count=%s", exhausted.rowcount
        )
