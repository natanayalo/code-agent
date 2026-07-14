"""Persistence for Temporal activity handoff state."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from db.models import TemporalTaskState


class TemporalTaskStateRepository:
    """Store the latest durable orchestrator snapshot for a Temporal workflow."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, *, task_id: str) -> TemporalTaskState | None:
        return self.session.query(TemporalTaskState).filter_by(task_id=task_id).one_or_none()

    def upsert(self, *, task_id: str, state: dict[str, Any]) -> TemporalTaskState:
        record = self.get(task_id=task_id)
        if record is None:
            record = TemporalTaskState(task_id=task_id, state=state)
            self.session.add(record)
        else:
            record.state = state
        self.session.flush()
        return record

    def delete(self, *, task_id: str) -> None:
        record = self.get(task_id=task_id)
        if record is not None:
            self.session.delete(record)
            self.session.flush()
