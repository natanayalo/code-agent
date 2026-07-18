"""Persistence boundary for transactional Temporal command delivery."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
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

    def pending(self) -> list[TemporalCommand]:
        return list(
            self.session.scalars(
                select(TemporalCommand)
                .where(TemporalCommand.delivered_at.is_(None))
                .order_by(TemporalCommand.created_at.asc())
            )
        )

    def mark_delivered(self, command: TemporalCommand) -> None:
        command.delivered_at = utc_now()
        command.attempts += 1
        command.last_error = None

    def mark_failed(self, command: TemporalCommand, error: Exception) -> None:
        command.attempts += 1
        command.last_error = str(error)[:4000]
