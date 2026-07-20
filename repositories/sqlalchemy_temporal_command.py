"""Persistence boundary for transactional Temporal command delivery."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from db.base import utc_now
from db.models import Task, TemporalCommand


class TemporalCommandRepository:
    """Create and reconcile idempotent Temporal start, signal, and cancel commands."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(
        self,
        *,
        task_id: str,
        command_type: str,
        command_key: str,
        payload: dict[str, Any],
    ) -> None:
        self.session.execute(
            select(Task.id).where(Task.id == task_id).with_for_update()
        ).scalar_one()
        existing = self.session.scalar(
            select(TemporalCommand).where(TemporalCommand.command_key == command_key)
        )
        if existing:
            return
        for _ in range(3):
            next_sequence = self.session.scalar(
                select(func.coalesce(func.max(TemporalCommand.sequence_number), 0) + 1).where(
                    TemporalCommand.task_id == task_id
                )
            )
            try:
                with self.session.begin_nested():
                    self.session.add(
                        TemporalCommand(
                            task_id=task_id,
                            command_type=command_type,
                            command_key=command_key,
                            payload=payload,
                            sequence_number=int(next_sequence or 1),
                        )
                    )
                    self.session.flush()
                return
            except IntegrityError:
                existing = self.session.scalar(
                    select(TemporalCommand).where(TemporalCommand.command_key == command_key)
                )
                if existing:
                    return
        raise RuntimeError("Could not allocate a unique Temporal command sequence.")

    def claim_pending(self, *, limit: int, lease_seconds: int) -> list[TemporalCommand]:
        """Atomically fence a bounded batch before any Temporal network call."""
        self.session.flush()
        now = utc_now()
        eligible = and_(
            TemporalCommand.delivered_at.is_(None),
            TemporalCommand.dead_lettered_at.is_(None),
            TemporalCommand.superseded_at.is_(None),
            TemporalCommand.next_attempt_at <= now,
            or_(
                TemporalCommand.claim_token.is_(None),
                TemporalCommand.claim_expires_at.is_(None),
                TemporalCommand.claim_expires_at <= now,
            ),
        )
        earlier = aliased(TemporalCommand)
        ordered_eligible = and_(
            eligible,
            ~exists(
                select(1).where(
                    earlier.task_id == TemporalCommand.task_id,
                    earlier.sequence_number < TemporalCommand.sequence_number,
                    earlier.delivered_at.is_(None),
                    earlier.dead_lettered_at.is_(None),
                    earlier.superseded_at.is_(None),
                )
            ),
        )
        candidate_ids = list(
            self.session.scalars(
                select(TemporalCommand.id)
                .where(ordered_eligible)
                .order_by(TemporalCommand.created_at.asc(), TemporalCommand.sequence_number.asc())
                .limit(limit)
            )
        )
        claimed: list[TemporalCommand] = []
        for command_id in candidate_ids:
            claim_token = str(uuid4())
            updated = self.session.execute(
                update(TemporalCommand)
                .where(TemporalCommand.id == command_id, ordered_eligible)
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
                TemporalCommand.superseded_at.is_(None),
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
                TemporalCommand.superseded_at.is_(None),
            )
            .values(**values)
        )
        return bool(getattr(updated, "rowcount", 0))

    def supersede_for_cancel(self, *, task_id: str) -> bool:
        """Resolve pending task commands when cancellation wins before Temporal start."""
        start_pending = self.session.scalar(
            select(TemporalCommand.id).where(
                TemporalCommand.task_id == task_id,
                TemporalCommand.command_type == "start",
                TemporalCommand.delivered_at.is_(None),
            )
        )
        if start_pending is None:
            return False
        self.session.execute(
            update(TemporalCommand)
            .where(
                TemporalCommand.task_id == task_id,
                TemporalCommand.delivered_at.is_(None),
                TemporalCommand.dead_lettered_at.is_(None),
                TemporalCommand.superseded_at.is_(None),
            )
            .values(
                superseded_at=utc_now(),
                claim_token=None,
                claim_expires_at=None,
                last_error="Superseded by cancellation before Temporal workflow start.",
            )
        )
        return True
