"""Integration tests for reviewable memory proposal persistence."""

from __future__ import annotations

from db.enums import MemoryProposalCategory, MemoryProposalStatus
from repositories import (
    MemoryProposalRepository,
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    session_scope,
)


def test_memory_proposal_accept_upserts_personal_memory_idempotently(session_factory) -> None:
    """Accepting a personal memory proposal writes memory once and marks review metadata."""
    with session_scope(session_factory) as session:
        repo = MemoryProposalRepository(session)
        proposal = repo.create(
            category=MemoryProposalCategory.PERSONAL,
            memory_key="communication_preferences",
            value={"style": "concise"},
            source="operator",
            confidence=0.95,
            scope="global",
            requires_verification=False,
            title="Communication preference",
        )

        status, accepted, memory, detail = repo.accept(proposal.id)

        assert status == "accepted"
        assert detail is None
        assert accepted is not None
        assert accepted.status == MemoryProposalStatus.ACCEPTED
        assert accepted.accepted_memory_id == memory.id
        assert accepted.reviewed_at is not None

        second_status, second_accepted, second_memory, second_detail = repo.accept(proposal.id)

        stored = PersonalMemoryRepository(session).get(memory_key="communication_preferences")
        assert second_status == "already_accepted"
        assert second_detail is None
        assert second_accepted is not None
        assert second_accepted.accepted_memory_id == accepted.accepted_memory_id
        assert second_memory is not None
        assert second_memory.id == memory.id
        assert stored is not None
        assert stored.id == memory.id
        assert stored.value == {"style": "concise"}
        assert stored.source == "operator"
        assert stored.confidence == 0.95
        assert stored.scope == "global"
        assert stored.requires_verification is False


def test_memory_proposal_accept_upserts_project_memory(session_factory) -> None:
    """Project memory proposals upsert through the project memory repository."""
    repo_url = "https://github.com/natanayalo/code-agent"
    with session_scope(session_factory) as session:
        repo = MemoryProposalRepository(session)
        proposal = repo.create(
            category="project",
            repo_url=repo_url,
            memory_key="verification_commands",
            value={"python": ".venv/bin/pytest tests/unit"},
            source="curated_corpus",
            scope="repo",
        )

        status, accepted, memory, detail = repo.accept(proposal.id)

        stored = ProjectMemoryRepository(session).get(
            repo_url=repo_url,
            memory_key="verification_commands",
        )
        assert status == "accepted"
        assert detail is None
        assert accepted is not None
        assert accepted.accepted_memory_id == memory.id
        assert stored is not None
        assert stored.id == memory.id
        assert stored.value == {"python": ".venv/bin/pytest tests/unit"}


def test_memory_proposal_list_filters_and_reject_terminal_status(session_factory) -> None:
    """Proposal review lifecycle supports filters and rejected rows are terminal."""
    repo_url = "https://github.com/natanayalo/code-agent"
    with session_scope(session_factory) as session:
        repo = MemoryProposalRepository(session)
        pending_personal = repo.create(
            category="personal",
            memory_key="tone",
            value={"style": "direct"},
        )
        rejected_project = repo.create(
            category="project",
            repo_url=repo_url,
            memory_key="pitfall",
            value={"note": "use repo venv"},
        )

        reject_status, rejected, reject_detail = repo.reject(rejected_project.id)
        accept_status, accepted_after_reject, _memory, accept_detail = repo.accept(
            rejected_project.id
        )

        assert reject_status == "rejected"
        assert reject_detail is None
        assert rejected is not None
        assert rejected.status == MemoryProposalStatus.REJECTED
        assert accept_status == "conflict"
        assert accepted_after_reject is not None
        assert "cannot be accepted" in (accept_detail or "")
        assert repo.list(status="pending_review") == [pending_personal]
        assert repo.list(status="rejected", category="project", repo_url=repo_url) == [
            rejected_project
        ]


def test_memory_proposal_missing_rows_return_not_found(session_factory) -> None:
    """Reviewing missing proposals should return not_found instead of raising."""
    with session_scope(session_factory) as session:
        repo = MemoryProposalRepository(session)

        accept_status, accept_proposal, accept_memory, accept_detail = repo.accept("missing")
        reject_status, reject_proposal, reject_detail = repo.reject("missing")

        assert accept_status == "not_found"
        assert accept_proposal is None
        assert accept_memory is None
        assert "not found" in (accept_detail or "")
        assert reject_status == "not_found"
        assert reject_proposal is None
        assert "not found" in (reject_detail or "")
