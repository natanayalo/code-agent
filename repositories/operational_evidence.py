"""Read-only, bounded queries for scheduled operational evidence."""

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.readiness_alert_models import (
    ReadinessAlertAcknowledgment,
    ReadinessAlertDelivery,
    ReadinessAlertState,
)
from db.webhook_alert_models import WebhookAlertDelivery

SOURCES: dict[str, tuple[Any, Any]] = {
    "telegram_deliveries": (ReadinessAlertDelivery, ReadinessAlertDelivery.observed_at),
    "webhook_deliveries": (WebhookAlertDelivery, WebhookAlertDelivery.observed_at),
    "acknowledgments": (ReadinessAlertAcknowledgment, ReadinessAlertAcknowledgment.acknowledged_at),
}


def read_source(
    session: Session, name: str, start: datetime, end: datetime
) -> tuple[list[Any], int]:
    model, timestamp = SOURCES[name]
    condition = (timestamp >= start, timestamp < end)
    total = session.scalar(select(func.count()).select_from(model).where(*condition)) or 0
    rows: list[Any] = list(
        session.scalars(
            select(model).where(*condition).order_by(timestamp.desc(), model.id.asc()).limit(100)
        )
    )
    return rows, total


def latest_observation(session: Session, cutoff: datetime) -> datetime | None:
    return session.scalar(
        select(func.max(ReadinessAlertState.last_checked_at)).where(
            ReadinessAlertState.last_checked_at < cutoff
        )
    )
