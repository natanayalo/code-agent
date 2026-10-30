"""Durable webhook notification evidence."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WebhookAlertDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A transition targeted to one opaque destination fingerprint."""

    __tablename__ = "webhook_alert_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "destination_id", "reason", "transition_number", name="uq_webhook_alert_transition"
        ),
        Index("ix_webhook_alert_due", "destination_id", "status", "next_attempt_at"),
    )

    destination_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    transition_number: Mapped[int] = mapped_column(Integer, nullable=False)
    transition: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_code: Mapped[str | None] = mapped_column(String(64))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
