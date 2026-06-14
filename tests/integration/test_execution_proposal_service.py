"""Integration tests for execution proposal service methods."""

import pytest

from db.enums import ProposalStatus
from db.models import Session as ConversationSession
from db.models import User
from repositories import ProposalRepository, session_scope


@pytest.fixture
def test_session_id(session_factory):
    with session_scope(session_factory) as session:
        user = User(external_user_id="test_user_id")
        session.add(user)
        session.flush()
        conversation_session = ConversationSession(
            user_id=user.id,
            channel="http",
            external_thread_id="test-thread",
        )
        session.add(conversation_session)
        session.flush()
        return conversation_session.id


@pytest.fixture
def pending_proposal_id(session_factory, test_session_id):
    with session_scope(session_factory) as session:
        repo = ProposalRepository(session)
        proposal = repo.create_proposal(
            session_id=test_session_id,
            title="Test Idea",
            summary="A test idea summary",
            content="Some details here",
            status=ProposalStatus.PENDING_REVIEW,
        )
        return proposal.id


def test_list_proposals(client, pending_proposal_id):
    task_service = client.app.state.task_service
    proposals = task_service.list_proposals(status=ProposalStatus.PENDING_REVIEW)
    assert len(proposals) > 0
    assert any(p.proposal_id == pending_proposal_id for p in proposals)


def test_accept_proposal_success(client, pending_proposal_id):
    task_service = client.app.state.task_service
    status, task_snapshot, detail = task_service.accept_proposal(pending_proposal_id)
    assert status == "created"
    assert task_snapshot is not None
    assert detail is None

    # Check that task was created with correct text
    assert "Test Idea" in task_snapshot.task_text
    assert "A test idea summary" in task_snapshot.task_text

    # Check idempotency
    status2, task_snapshot2, detail2 = task_service.accept_proposal(pending_proposal_id)
    assert status2 == "conflict"
    assert task_snapshot2 is not None
    assert task_snapshot2.task_id == task_snapshot.task_id
    assert "already accepted" in detail2


def test_reject_proposal_success(client, pending_proposal_id):
    task_service = client.app.state.task_service
    status, proposal_snapshot, detail = task_service.reject_proposal(pending_proposal_id)
    assert status == "success"
    assert proposal_snapshot is not None
    assert proposal_snapshot.status == "rejected"

    # Reject again (idempotent)
    status2, proposal_snapshot2, detail2 = task_service.reject_proposal(pending_proposal_id)
    assert status2 == "success"
    assert proposal_snapshot2.status == "rejected"


def test_accept_proposal_not_found(client):
    task_service = client.app.state.task_service
    status, snapshot, detail = task_service.accept_proposal("00000000-0000-0000-0000-000000000000")
    assert status == "not_found"


def test_accept_proposal_invalid_status(client, session_factory, test_session_id):
    task_service = client.app.state.task_service
    with session_scope(session_factory) as session:
        repo = ProposalRepository(session)
        proposal = repo.create_proposal(
            session_id=test_session_id,
            title="Implemented Idea",
            summary="Summary",
            status=ProposalStatus.IMPLEMENTED,
        )
        proposal_id = proposal.id

    status, snapshot, detail = task_service.accept_proposal(proposal_id)
    assert status == "conflict"
    assert "cannot be accepted" in detail


def test_accept_proposal_scout_metadata(client, session_factory, test_session_id):
    task_service = client.app.state.task_service
    with session_scope(session_factory) as session:
        repo = ProposalRepository(session)
        proposal = repo.create_proposal(
            session_id=test_session_id,
            title="Scout Idea",
            summary="Scout Summary",
            status=ProposalStatus.PENDING_REVIEW,
            metadata_payload={
                "diff_text": "-a\n+b",
                "files_changed": ["a.txt"],
                "json_payload": {"hello": "world"},
            },
        )
        proposal_id = proposal.id

    status, snapshot, detail = task_service.accept_proposal(proposal_id)
    assert status == "created"
    assert snapshot is not None
    assert "```diff\n-a\n+b\n```" in snapshot.task_text
    assert "Files changed:\na.txt" in snapshot.task_text
    assert '```json\n{\n  "hello": "world"\n}\n```' in snapshot.task_text


def test_accept_proposal_concurrent_deletion(client, session_factory, test_session_id):
    task_service = client.app.state.task_service
    with session_scope(session_factory) as session:
        repo = ProposalRepository(session)
        proposal = repo.create_proposal(
            session_id=test_session_id,
            title="To be deleted",
            summary="Will be deleted during accept",
            status=ProposalStatus.PENDING_REVIEW,
        )
        proposal_id = proposal.id

    # We need to simulate concurrent deletion after task creation and after
    # the proposal is initially fetched inside the transaction, but before the UPDATE.
    # We can do this by patching ProposalRepository.get_proposal to delete the proposal
    # from a separate transaction right before it returns, but ONLY on the second call
    # (since accept_proposal fetches the proposal once before creating the task, and once after).
    from repositories.sqlalchemy_proposal import ProposalRepository as RealRepo

    original_get_proposal = RealRepo.get_proposal

    call_count = [0]

    def mock_get_proposal(self, pid):
        call_count[0] += 1
        p = original_get_proposal(self, pid)
        if pid == proposal_id and call_count[0] == 2:
            with session_scope(session_factory) as del_session:
                del_session.execute(
                    __import__("sqlalchemy").text("DELETE FROM proposals WHERE id = :id"),
                    {"id": pid},
                )
        return p

    RealRepo.get_proposal = mock_get_proposal

    original_cancel_task = task_service.cancel_task
    cancel_called = []

    def mock_cancel_task(*, task_id: str) -> None:
        cancel_called.append(task_id)
        original_cancel_task(task_id=task_id)

    task_service.cancel_task = mock_cancel_task

    try:
        status, snapshot, detail = task_service.accept_proposal(proposal_id)
        assert status == "not_found"
        assert "deleted concurrently" in detail

        # Verify the created task was cancelled by checking if cancel_task was called
        assert len(cancel_called) == 1

        # Find the task created for this test to verify the DB status if possible
        with session_scope(session_factory) as session:
            from repositories.sqlalchemy_task import TaskRepository

            tasks = TaskRepository(session).list_by_session(session_id=test_session_id)
            # It seems cancel_task might set status to FAILED if the task hasn't fully started,
            # or maybe CANCELLED. Either way, cancel_task was invoked.
            assert all(t.status in ("cancelled", "failed") for t in tasks)
    finally:
        RealRepo.get_proposal = original_get_proposal
        task_service.cancel_task = original_cancel_task
