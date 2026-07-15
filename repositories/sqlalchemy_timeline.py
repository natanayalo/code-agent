"""Task-timeline SQLAlchemy repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.base import utc_now
from db.enums import TimelineEventType
from db.models import TaskTimelineEvent


class TaskTimelineRepository:
    """Persist and query task timeline events (T-090)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        task_id: str,
        event_type: str | TimelineEventType,
        attempt_number: int = 0,
        sequence_number: int = 0,
        event_key: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> TaskTimelineEvent:
        if event_key:
            existing = self.session.scalar(
                select(TaskTimelineEvent).where(
                    TaskTimelineEvent.task_id == task_id,
                    TaskTimelineEvent.event_key == event_key,
                )
            )
            if existing is not None:
                return existing
        event = TaskTimelineEvent(
            task_id=task_id,
            attempt_number=attempt_number,
            sequence_number=sequence_number,
            event_key=event_key,
            event_type=event_type,
            message=message,
            payload=payload,
        )
        if created_at is not None:
            event.created_at = created_at
            event.updated_at = created_at
        self.session.add(event)
        self.session.flush()
        return event

    def list_by_task(self, task_id: str) -> list[TaskTimelineEvent]:
        statement = (
            select(TaskTimelineEvent)
            .where(TaskTimelineEvent.task_id == task_id)
            .order_by(
                TaskTimelineEvent.attempt_number.asc(), TaskTimelineEvent.sequence_number.asc()
            )
        )
        return list(self.session.scalars(statement))

    def count_by_attempt(self, task_id: str, attempt_number: int) -> int:
        return (
            self.session.scalar(
                select(func.count())
                .select_from(TaskTimelineEvent)
                .where(
                    TaskTimelineEvent.task_id == task_id,
                    TaskTimelineEvent.attempt_number == attempt_number,
                )
            )
            or 0
        )

    def create_next_for_attempt(
        self,
        *,
        task_id: str,
        attempt_number: int,
        event_type: str | TimelineEventType,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        created_at: datetime | None = None,
        max_retries: int = 3,
        event_key: str | None = None,
    ) -> TaskTimelineEvent:
        tries = 0
        while True:
            sequence_number = self.count_by_attempt(task_id=task_id, attempt_number=attempt_number)
            try:
                with self.session.begin_nested():
                    event = self.create(
                        task_id=task_id,
                        attempt_number=attempt_number,
                        sequence_number=sequence_number,
                        event_key=event_key,
                        event_type=event_type,
                        message=message,
                        payload=payload,
                        created_at=created_at,
                    )
                return event
            except IntegrityError:
                tries += 1
                if tries >= max_retries:
                    raise

    def create_batch(
        self,
        *,
        task_id: str,
        events: list[dict[str, Any]],
    ) -> None:
        if not events:
            return

        now = utc_now()
        params = []
        for event in events:
            created_at = event.get("created_at") if event.get("created_at") is not None else now
            params.append(
                {
                    "id": uuid4().hex,
                    "task_id": task_id,
                    "attempt_number": event["attempt_number"],
                    "sequence_number": event["sequence_number"],
                    "event_type": event["event_type"],
                    "message": event.get("message"),
                    "payload": event.get("payload"),
                    "created_at": created_at,
                    "updated_at": created_at,
                }
            )

        self.session.execute(insert(TaskTimelineEvent), params)
        self.session.flush()
