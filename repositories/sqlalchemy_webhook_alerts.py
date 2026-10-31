"""Lease and complete destination-specific webhook deliveries."""

from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, aliased

from db.base import generate_uuid, utc_now
from db.readiness_alert_models import ReadinessAlertAcknowledgment
from db.webhook_alert_models import WebhookAlertDelivery as Delivery
from repositories.sqlalchemy_readiness_alerts import DELIVERY_LEASE_SECONDS, MAX_DELIVERY_ATTEMPTS


class WebhookAlertRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def claim_one(self, destination_id: str) -> Delivery | None:
        now = utc_now()
        earlier = aliased(Delivery)
        blocked = (
            select(earlier.id)
            .where(
                earlier.destination_id == Delivery.destination_id,
                earlier.reason == Delivery.reason,
                earlier.transition_number < Delivery.transition_number,
                earlier.status.in_(("pending", "processing")),
            )
            .exists()
        )
        # Expired final attempts are terminal, including after a crash.
        self.session.execute(
            update(Delivery)
            .where(
                Delivery.destination_id == destination_id,
                Delivery.status == "processing",
                Delivery.lease_expires_at <= now,
                Delivery.attempts >= MAX_DELIVERY_ATTEMPTS,
            )
            .values(
                status="failed",
                lease_owner=None,
                lease_expires_at=None,
                last_failure_code="webhook_attempts_exhausted",
            )
        )
        item = self.session.scalar(
            select(Delivery)
            .where(
                Delivery.destination_id == destination_id,
                Delivery.attempts < MAX_DELIVERY_ATTEMPTS,
                or_(
                    (Delivery.status == "pending") & (Delivery.next_attempt_at <= now),
                    (Delivery.status == "processing") & (Delivery.lease_expires_at <= now),
                ),
                ~blocked,
            )
            .order_by(Delivery.created_at, Delivery.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if item is not None:
            item.status = "processing"
            item.attempts += 1
            item.lease_owner = generate_uuid()
            item.lease_expires_at = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
            self.session.flush()
        return item

    def complete(self, delivery_id: str, owner: str, status: int | None) -> None:
        item = self.session.scalar(
            select(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "processing",
                Delivery.lease_owner == owner,
            )
            .with_for_update()
        )
        if item is None:
            return
        item.lease_owner = None
        item.lease_expires_at = None
        item.last_http_status = status
        if status is not None and 200 <= status < 300:
            item.status = "delivered"
            item.delivered_at = utc_now()
            item.last_failure_code = None
            return
        item.last_failure_code = "webhook_request_failed"
        acknowledged = self.session.scalar(
            select(ReadinessAlertAcknowledgment.id).where(
                ReadinessAlertAcknowledgment.reason == item.reason,
                ReadinessAlertAcknowledgment.transition_number == item.transition_number,
            )
        )
        if acknowledged is not None:
            item.status = "suppressed"
            return
        retryable = status is None or status in (408, 429) or 500 <= status < 600
        if not retryable or item.attempts >= MAX_DELIVERY_ATTEMPTS:
            item.status = "failed"
        else:
            item.status = "pending"
            item.next_attempt_at = utc_now() + timedelta(
                seconds=min(5 * 2 ** (item.attempts - 1), 900)
            )
