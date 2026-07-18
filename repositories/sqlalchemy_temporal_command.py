"""Persistence boundary for transactional Temporal command delivery."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from db.base import utc_now
from db.models import TemporalCommand


class TemporalCommandRepository:
    """Create and reconcile idempotent Temporal start, signal, and cancel commands."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self, *, task_id: str, command_type: str, command_key: str, payload: dict[str, Any]
    ) -> None:
        existing = self.session.scalar(
            select(TemporalCommand).where(TemporalCommand.command_key == command_key)
        )
        if existing:
            return
        self.session.add(
            TemporalCommand(
                task_id=task_id,
                command_type=command_type,
                command_key=command_key,
                payload=payload,
            )
        )

    def claim_pending(self, *, limit: int, lease_seconds: int) -> list[TemporalCommand]:
        """Atomically fence a bounded batch before any Temporal network call."""
        now = utc_now()
        eligible = and_(
            TemporalCommand.delivered_at.is_(None),
            TemporalCommand.dead_lettered_at.is_(None),
            TemporalCommand.next_attempt_at <= now,
            or_(
                TemporalCommand.claim_token.is_(None),
                TemporalCommand.claim_expires_at.is_(None),
                TemporalCommand.claim_expires_at <= now,
            ),
        )
        candidate_ids = list(
            self.session.scalars(
                select(TemporalCommand.id)
                .where(eligible)
                .order_by(TemporalCommand.created_at.asc())
                .limit(limit)
            )
        )
        claimed: list[TemporalCommand] = []
        for command_id in candidate_ids:
            claim_token = str(uuid4())
            updated = self.session.execute(
                update(TemporalCommand)
                .where(TemporalCommand.id == command_id, eligible)
                .values(
                    claim_token=claim_token,
                    claim_expires_at=now + timedelta(seconds=lease_seconds),
                )
            )
            if getattr(updated, "rowcount", 0):
                command = self.session.get(TemporalCommand, command_id)
                if command is not None:
                    claimed.append(command)
        self.session.flush()
        return claimed

    def mark_delivered(self, *, command_id: str, claim_token: str) -> bool:
        """Acknowledge only the dispatcher instance that owns the active claim."""
        updated = self.session.execute(
            update(TemporalCommand)
            .where(
                TemporalCommand.id == command_id,
                TemporalCommand.claim_token == claim_token,
                TemporalCommand.delivered_at.is_(None),
            )
            .values(
                delivered_at=utc_now(),
                claim_token=None,
                claim_expires_at=None,
                last_error=None,
            )
        )
        return bool(getattr(updated, "rowcount", 0))

    def mark_failed(
        self,
        *,
        command_id: str,
        claim_token: str,
        error: Exception,
        retry_at: Any | None,
    ) -> bool:
        """Record a fenced retry or terminal dead-letter result."""
        values: dict[str, Any] = {
            "attempts": TemporalCommand.attempts + 1,
            "last_error": str(error)[:4000],
            "claim_token": None,
            "claim_expires_at": None,
        }
        if retry_at is None:
            values["dead_lettered_at"] = utc_now()
        else:
            values["next_attempt_at"] = retry_at
        updated = self.session.execute(
            update(TemporalCommand)
            .where(
                TemporalCommand.id == command_id,
                TemporalCommand.claim_token == claim_token,
                TemporalCommand.delivered_at.is_(None),
            )
            .values(**values)
        )
        return bool(getattr(updated, "rowcount", 0))
