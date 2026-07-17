"""Durable fixed-width permits for Temporal execution activities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.base import generate_uuid, utc_now
from db.models import ExecutionCapacityPermit


class ExecutionCapacityPermitRepository:
    """Lease one of two queue-scoped capacity slots across worker processes."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def claim(self, *, queue_name: str, owner: str, lease_seconds: int = 60) -> bool:
        now = utc_now()
        permits = list(
            self.session.scalars(
                select(ExecutionCapacityPermit)
                .where(ExecutionCapacityPermit.queue_name == queue_name)
                .with_for_update()
            )
        )
        while len(permits) < 2:
            new_permit = ExecutionCapacityPermit(
                id=generate_uuid(), queue_name=queue_name, slot_index=len(permits)
            )
            self.session.add(new_permit)
            permits.append(new_permit)
        permit: ExecutionCapacityPermit | None = None
        for item in permits:
            if (
                item.lease_owner == owner
                or item.lease_expires_at is None
                or _is_expired(item.lease_expires_at, now)
            ):
                permit = item
                break
        if permit is None:
            return False
        permit.lease_owner = owner
        permit.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return True

    def release(self, *, owner: str) -> None:
        permit = self.session.scalar(
            select(ExecutionCapacityPermit).where(ExecutionCapacityPermit.lease_owner == owner)
        )
        if permit is not None:
            permit.lease_owner = None
            permit.lease_expires_at = None


def _is_expired(expires_at: datetime | None, now: datetime) -> bool:
    """Handle SQLite's timezone-naive round trip in local test databases."""
    if expires_at is None:
        return True
    if getattr(expires_at, "tzinfo", None) is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now
