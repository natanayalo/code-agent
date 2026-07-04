"""SQLAlchemy repository for memory admission decision records."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import MemoryAdmissionDecision


class MemoryAdmissionDecisionRepository:
    """Persist inspectable outcomes from the memory admission boundary."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        category: str,
        memory_key: str,
        candidate_payload: dict[str, Any],
        decision: str,
        risk_level: str,
        reason: str,
        task_id: str | None = None,
        session_id: str | None = None,
        durable_memory_id: str | None = None,
        proposal_id: str | None = None,
        source_observation_id: str | None = None,
    ) -> MemoryAdmissionDecision:
        row = MemoryAdmissionDecision(
            category=category,
            memory_key=memory_key,
            candidate_payload=candidate_payload,
            decision=decision,
            risk_level=risk_level,
            reason=reason,
            task_id=task_id,
            session_id=session_id,
            durable_memory_id=durable_memory_id,
            proposal_id=proposal_id,
            source_observation_id=source_observation_id,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryAdmissionDecision]:
        statement = select(MemoryAdmissionDecision)
        if task_id is not None:
            statement = statement.where(MemoryAdmissionDecision.task_id == task_id)
        if session_id is not None:
            statement = statement.where(MemoryAdmissionDecision.session_id == session_id)
        statement = (
            statement.order_by(
                MemoryAdmissionDecision.created_at.desc(),
                MemoryAdmissionDecision.id.desc(),
            )
            .limit(max(0, limit))
            .offset(max(0, offset))
        )
        return list(self.session.scalars(statement))
