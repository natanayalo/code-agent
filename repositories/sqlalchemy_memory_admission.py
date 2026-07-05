"""SQLAlchemy repository for memory admission decision records."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from db.models import MemoryAdmissionDecision, MemoryObservation, Task


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
        decision: str | None = None,
        source_observation_id: str | None = None,
        repo_url: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryAdmissionDecision]:
        statement = select(MemoryAdmissionDecision)
        if task_id is not None:
            statement = statement.where(MemoryAdmissionDecision.task_id == task_id)
        if session_id is not None:
            statement = statement.where(MemoryAdmissionDecision.session_id == session_id)
        if decision is not None:
            statement = statement.where(MemoryAdmissionDecision.decision == decision)
        if source_observation_id is not None:
            statement = statement.where(
                MemoryAdmissionDecision.source_observation_id == source_observation_id
            )
        if repo_url is not None:
            repo_url_expr = func.coalesce(
                cast(MemoryAdmissionDecision.candidate_payload["repo_url"].as_string(), String),
                MemoryObservation.repo_url,
                Task.repo_url,
            )
            statement = statement.outerjoin(
                MemoryObservation,
                MemoryObservation.id == MemoryAdmissionDecision.source_observation_id,
            ).outerjoin(Task, Task.id == MemoryAdmissionDecision.task_id)
            statement = statement.where(repo_url_expr == repo_url)
        statement = (
            statement.order_by(
                MemoryAdmissionDecision.created_at.desc(),
                MemoryAdmissionDecision.id.desc(),
            )
            .limit(max(0, limit))
            .offset(max(0, offset))
        )
        return list(self.session.scalars(statement))

    def list_for_source_observation_ids(
        self,
        observation_ids: set[str],
    ) -> Sequence[MemoryAdmissionDecision]:
        """Fetch decisions for a specific batch of source observation ids."""
        if not observation_ids:
            return []

        statement = (
            select(MemoryAdmissionDecision)
            .where(MemoryAdmissionDecision.source_observation_id.in_(observation_ids))
            .order_by(
                MemoryAdmissionDecision.created_at.desc(),
                MemoryAdmissionDecision.id.desc(),
            )
        )
        return list(self.session.scalars(statement))
