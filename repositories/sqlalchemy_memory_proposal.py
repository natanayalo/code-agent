"""SQLAlchemy-backed repository for reviewable memory proposals."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.base import utc_now
from db.enums import MemoryProposalCategory, MemoryProposalStatus
from db.models import MemoryProposal, PersonalMemory, ProjectMemory
from repositories.sqlalchemy_memory import PersonalMemoryRepository, ProjectMemoryRepository


class MemoryProposalRepository:
    """Persist and review memory candidates before writing durable memory."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        category: MemoryProposalCategory | str,
        memory_key: str,
        value: dict[str, Any],
        repo_url: str | None = None,
        source: str | None = None,
        confidence: float = 1.0,
        scope: str | None = None,
        requires_verification: bool = True,
        title: str | None = None,
        summary: str | None = None,
        evidence: dict[str, Any] | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        source_observation_id: str | None = None,
    ) -> MemoryProposal:
        normalized_category = MemoryProposalCategory(category)
        normalized_repo_url = (repo_url or "").strip() or None
        normalized_memory_key = memory_key.strip()
        self._validate_category_scope(category=normalized_category, repo_url=normalized_repo_url)
        proposal = MemoryProposal(
            category=normalized_category,
            repo_url=normalized_repo_url,
            memory_key=normalized_memory_key,
            value=value,
            source=source,
            confidence=confidence,
            scope=scope,
            requires_verification=requires_verification,
            status=MemoryProposalStatus.PENDING_REVIEW,
            title=title,
            summary=summary,
            evidence=evidence,
            task_id=task_id,
            session_id=session_id,
            source_observation_id=source_observation_id,
        )
        self.session.add(proposal)
        self.session.flush()
        return proposal

    def get(self, proposal_id: str) -> MemoryProposal | None:
        return self.session.get(MemoryProposal, proposal_id)

    def list(
        self,
        *,
        status: MemoryProposalStatus | str | Sequence[MemoryProposalStatus | str] | None = None,
        category: MemoryProposalCategory | str | None = None,
        repo_url: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryProposal]:
        statement = select(MemoryProposal)
        statuses = self._normalize_statuses(status)
        if statuses is not None:
            if not statuses:
                return []
            statement = statement.where(MemoryProposal.status.in_(statuses))
        if category is not None:
            statement = statement.where(MemoryProposal.category == MemoryProposalCategory(category))
        if repo_url is not None:
            statement = statement.where(MemoryProposal.repo_url == repo_url)
        if task_id is not None:
            statement = statement.where(MemoryProposal.task_id == task_id)
        if session_id is not None:
            statement = statement.where(MemoryProposal.session_id == session_id)
        statement = (
            statement.order_by(MemoryProposal.updated_at.desc(), MemoryProposal.id.desc())
            .limit(max(0, limit))
            .offset(max(0, offset))
        )
        return list(self.session.scalars(statement))

    @staticmethod
    def _normalize_statuses(
        status: MemoryProposalStatus | str | Sequence[MemoryProposalStatus | str] | None,
    ) -> Sequence[MemoryProposalStatus] | None:
        if status is None:
            return None
        if isinstance(status, MemoryProposalStatus | str):
            return [MemoryProposalStatus(status)]
        return [MemoryProposalStatus(item) for item in status]

    @staticmethod
    def _validate_category_scope(
        *,
        category: MemoryProposalCategory,
        repo_url: str | None,
    ) -> None:
        if category == MemoryProposalCategory.PROJECT and not repo_url:
            raise ValueError("repo_url is required for project memory proposals.")
        if category == MemoryProposalCategory.PERSONAL and repo_url is not None:
            raise ValueError("repo_url must be omitted for personal memory proposals.")

    def accept(
        self,
        proposal_id: str,
        *,
        reviewed_at: datetime | None = None,
    ) -> tuple[
        Literal["accepted", "already_accepted", "conflict", "not_found"],
        MemoryProposal | None,
        PersonalMemory | ProjectMemory | None,
        str | None,
    ]:
        proposal = self.get(proposal_id)
        if proposal is None:
            return "not_found", None, None, f"Memory proposal '{proposal_id}' was not found."

        if proposal.status == MemoryProposalStatus.ACCEPTED:
            memory = self._get_accepted_memory(proposal)
            return "already_accepted", proposal, memory, None

        if proposal.status != MemoryProposalStatus.PENDING_REVIEW:
            return (
                "conflict",
                proposal,
                None,
                f"Memory proposal cannot be accepted from status '{proposal.status}'.",
            )

        memory = self._upsert_memory(proposal)
        proposal.status = MemoryProposalStatus.ACCEPTED
        proposal.accepted_memory_id = memory.id
        proposal.reviewed_at = reviewed_at or utc_now()
        self.session.flush()
        return "accepted", proposal, memory, None

    def reject(
        self,
        proposal_id: str,
        *,
        reviewed_at: datetime | None = None,
    ) -> tuple[
        Literal["rejected", "already_rejected", "conflict", "not_found"],
        MemoryProposal | None,
        str | None,
    ]:
        proposal = self.get(proposal_id)
        if proposal is None:
            return "not_found", None, f"Memory proposal '{proposal_id}' was not found."

        if proposal.status == MemoryProposalStatus.REJECTED:
            return "already_rejected", proposal, None

        if proposal.status != MemoryProposalStatus.PENDING_REVIEW:
            return (
                "conflict",
                proposal,
                f"Memory proposal cannot be rejected from status '{proposal.status}'.",
            )

        proposal.status = MemoryProposalStatus.REJECTED
        proposal.reviewed_at = reviewed_at or utc_now()
        self.session.flush()
        return "rejected", proposal, None

    def _upsert_memory(self, proposal: MemoryProposal) -> PersonalMemory | ProjectMemory:
        if proposal.category == MemoryProposalCategory.PERSONAL:
            return PersonalMemoryRepository(self.session).upsert(
                memory_key=proposal.memory_key,
                value=dict(proposal.value or {}),
                source=proposal.source,
                confidence=proposal.confidence,
                scope=proposal.scope,
                requires_verification=proposal.requires_verification,
            )

        if not proposal.repo_url:
            raise ValueError("Project memory proposals require repo_url.")
        return ProjectMemoryRepository(self.session).upsert(
            repo_url=proposal.repo_url,
            memory_key=proposal.memory_key,
            value=dict(proposal.value or {}),
            source=proposal.source,
            confidence=proposal.confidence,
            scope=proposal.scope,
            requires_verification=proposal.requires_verification,
        )

    def _get_accepted_memory(
        self,
        proposal: MemoryProposal,
    ) -> PersonalMemory | ProjectMemory | None:
        if proposal.accepted_memory_id is None:
            return None
        if proposal.category == MemoryProposalCategory.PERSONAL:
            return self.session.get(PersonalMemory, proposal.accepted_memory_id)
        return self.session.get(ProjectMemory, proposal.accepted_memory_id)
