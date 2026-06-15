"""Integration tests for Proposal persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from db.enums import ProposalStatus
from repositories.session import session_scope
from repositories.sqlalchemy import ProposalRepository, SessionRepository, UserRepository


def test_create_and_get_proposal(session_factory) -> None:
    """Can create and retrieve a proposal."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        proposal_repo = ProposalRepository(session)

        user = user_repo.create(
            external_user_id="github:test-proposal-1",
            display_name="Test User",
        )
        sess = session_repo.create(
            user_id=user.id,
            channel="web",
            external_thread_id="thread-proposal-1",
        )

        proposal = proposal_repo.create_proposal(
            session_id=sess.id,
            title="Test Proposal",
            summary="A test proposal",
            content="Detailed content",
            metadata_payload={"source": "scout"},
        )
        assert proposal.id is not None
        assert proposal.status == ProposalStatus.PENDING_REVIEW

        retrieved = proposal_repo.get_proposal(proposal.id)
        assert retrieved.id == proposal.id
        assert retrieved.title == "Test Proposal"
        assert retrieved.content == "Detailed content"
        assert retrieved.metadata_payload == {"source": "scout"}


def test_update_proposal_status(session_factory) -> None:
    """Can update a proposal's status."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        proposal_repo = ProposalRepository(session)

        user = user_repo.create(
            external_user_id="github:test-proposal-2",
            display_name="Test User",
        )
        sess = session_repo.create(
            user_id=user.id,
            channel="web",
            external_thread_id="thread-proposal-2",
        )

        proposal = proposal_repo.create_proposal(
            session_id=sess.id,
            title="Status Test",
            summary="Testing status update",
        )

        updated = proposal_repo.update_proposal_status(proposal.id, ProposalStatus.ACCEPTED)
        assert updated.status == ProposalStatus.ACCEPTED

        retrieved = proposal_repo.get_proposal(proposal.id)
        assert retrieved.status == ProposalStatus.ACCEPTED


def test_list_proposals(session_factory) -> None:
    """Can list proposals with filtering."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        proposal_repo = ProposalRepository(session)

        user = user_repo.create(
            external_user_id="github:test-proposal-3",
            display_name="Test User",
        )
        sess = session_repo.create(
            user_id=user.id,
            channel="web",
            external_thread_id="thread-proposal-3",
        )

        p1 = proposal_repo.create_proposal(
            session_id=sess.id,
            title="P1",
            summary="P1 summary",
        )
        p2 = proposal_repo.create_proposal(
            session_id=sess.id,
            title="P2",
            summary="P2 summary",
        )

        p1.created_at = datetime.now(UTC) - timedelta(seconds=5)
        p2.created_at = datetime.now(UTC)
        p3 = proposal_repo.create_proposal(
            session_id=sess.id,
            title="P3",
            summary="P3 summary",
            proposal_type="reflection",
        )
        p3.created_at = datetime.now(UTC) + timedelta(seconds=5)
        session.flush()

        proposal_repo.update_proposal_status(p1.id, ProposalStatus.REJECTED)

        all_props = proposal_repo.list_proposals(session_id=sess.id)
        assert len(all_props) == 3

        # Should be ordered newest first
        assert all_props[0].id == p3.id
        assert all_props[1].id == p2.id
        assert all_props[2].id == p1.id

        pending_props = proposal_repo.list_proposals(
            session_id=sess.id, status=ProposalStatus.PENDING_REVIEW
        )
        assert len(pending_props) == 2

        reflection_props = proposal_repo.list_proposals(
            session_id=sess.id, proposal_type="reflection"
        )
        assert len(reflection_props) == 1
        assert reflection_props[0].id == p3.id


def test_get_proposal_not_found(session_factory) -> None:
    """Raises ValueError for unknown proposal ID."""
    with session_scope(session_factory) as session:
        proposal_repo = ProposalRepository(session)
        with pytest.raises(ValueError):
            proposal_repo.get_proposal("unknown-id")


def test_create_proposal_rolls_back_with_session_scope(session_factory) -> None:
    """Proposal writes should participate in the caller-owned transaction."""
    proposal_id = ""

    with pytest.raises(RuntimeError, match="abort transaction"):
        with session_scope(session_factory) as session:
            user_repo = UserRepository(session)
            session_repo = SessionRepository(session)
            proposal_repo = ProposalRepository(session)

            user = user_repo.create(
                external_user_id="github:test-proposal-rollback",
                display_name="Rollback User",
            )
            sess = session_repo.create(
                user_id=user.id,
                channel="web",
                external_thread_id="thread-proposal-rollback",
            )
            proposal = proposal_repo.create_proposal(
                session_id=sess.id,
                title="Rollback",
                summary="Should not persist",
            )
            proposal_id = proposal.id
            raise RuntimeError("abort transaction")

    with session_scope(session_factory) as session:
        proposal_repo = ProposalRepository(session)
        with pytest.raises(ValueError):
            proposal_repo.get_proposal(proposal_id)
