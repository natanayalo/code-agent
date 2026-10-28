"""Durable state and delivery records for readiness transition alerts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin, utc_now


class ReadinessAlertState(TimestampMixin, Base):
    """Latest observed state and transition sequence for one stable reason."""

    __tablename__ = "readiness_alert_states"

    reason: Mapped[str] = mapped_column(String(80), primary_key=True)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    transition_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_transition_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReadinessAlertDelivery(TimestampMixin, Base):
    """One durable Telegram delivery for a readiness state transition."""

    __tablename__ = "readiness_alert_deliveries"
    __table_args__ = (
        UniqueConstraint("reason", "transition_number", name="uq_readiness_delivery_transition"),
        Index("ix_readiness_delivery_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reason: Mapped[str] = mapped_column(String(80), nullable=False)
    transition_number: Mapped[int] = mapped_column(Integer, nullable=False)
    transition: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_code: Mapped[str | None] = mapped_column(String(64))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
