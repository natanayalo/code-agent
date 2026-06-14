"""SQLAlchemy-backed repository for Proposal persistence."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.enums import ProposalStatus
from db.models import Proposal

logger = logging.getLogger(__name__)


class ProposalRepository:
    """Manages the persistence lifecycle for proposals (ideas)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_proposal(
        self,
        *,
        session_id: str,
        title: str,
        summary: str,
        task_id: str | None = None,
        content: str | None = None,
        status: ProposalStatus = ProposalStatus.PENDING_REVIEW,
        metadata_payload: dict[str, Any] | None = None,
    ) -> Proposal:
        """Create a new proposal tied to a session."""
        proposal = Proposal(
            session_id=session_id,
            task_id=task_id,
            title=title,
            summary=summary,
            content=content,
            status=status,
            metadata_payload=metadata_payload or {},
        )
        self.session.add(proposal)
        self.session.flush()
        return proposal

    def get_proposal(self, proposal_id: str) -> Proposal:
        """Retrieve a proposal by its ID."""
        proposal = self.session.get(Proposal, proposal_id)
        if proposal is None:
            raise ValueError(f"Proposal {proposal_id} not found")
        return proposal

    def update_proposal_status(self, proposal_id: str, status: ProposalStatus | str) -> Proposal:
        """Update the status of a proposal."""
        proposal = self.get_proposal(proposal_id)
        proposal.status = status
        self.session.flush()
        return proposal

    def list_proposals(
        self,
        *,
        status: ProposalStatus | str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Proposal]:
        """List proposals, ordered newest-first, with optional filtering."""
        limit = max(0, limit)
        offset = max(0, offset)

        stmt = select(Proposal)

        if status is not None:
            stmt = stmt.where(Proposal.status == ProposalStatus(status))
        if session_id is not None:
            stmt = stmt.where(Proposal.session_id == session_id)

        stmt = stmt.order_by(Proposal.created_at.desc(), Proposal.id.desc())
        stmt = stmt.limit(limit).offset(offset)

        return list(self.session.scalars(stmt))
