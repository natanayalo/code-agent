"""Unit tests for orchestrator/execution_proposal_service.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.base import Base
from db.enums import ProposalStatus, TaskStatus
from db.models import Proposal, Task, User
from db.models import Session as ConversationSession
from orchestrator import execution_proposal_service
from orchestrator.execution_types import CreateTaskOutcome, TaskSnapshot, TaskSubmission


class DummyExecutionService:
    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory
        self.canceled_task_ids: list[str] = []
        self.tasks: dict[str, TaskSnapshot] = {}
        self.fail_cancel = False

    def _map_to_snapshot(self, proposal: Proposal) -> Any:
        return execution_proposal_service._map_to_snapshot(self, proposal)

    def get_task(self, task_id: str) -> TaskSnapshot | None:
        return self.tasks.get(task_id)

    def create_task_outcome(
        self, submission: TaskSubmission, delivery_key: Any = None
    ) -> CreateTaskOutcome:
        deliv_id = delivery_key.delivery_id if delivery_key else "new"
        task_id = f"task_{submission.session.channel}_{deliv_id}"

        now = datetime.now(UTC)
        snapshot = TaskSnapshot(
            task_id=task_id,
            session_id="session-1",
            task_text=submission.task_text,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        self.tasks[task_id] = snapshot
        return CreateTaskOutcome(
            task_snapshot=snapshot,
            persisted=None,
            duplicate=False,
        )
        self.tasks[task_id] = snapshot
        return CreateTaskOutcome(
            task_snapshot=snapshot,
            created=True,
            duplicate=False,
        )

    def cancel_task(self, task_id: str) -> None:
        if self.fail_cancel:
            raise RuntimeError("Cancel failed")
        self.canceled_task_ids.append(task_id)


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(db_engine):
    return sessionmaker(bind=db_engine)


@pytest.fixture
def service(session_factory):
    return DummyExecutionService(session_factory)


def test_map_to_snapshot(session_factory, service):
    with session_factory() as session:
        user = User(external_user_id="u1", display_name="User 1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        proposal = Proposal(
            session_id=conv.id,
            title="Test Proposal",
            summary="Test Summary",
            content="Test Content",
            status=ProposalStatus.PENDING_REVIEW,
            proposal_type="scout",
            metadata_payload={"key": "value"},
        )
        session.add(proposal)
        session.commit()

        snapshot = service._map_to_snapshot(proposal)
        assert snapshot.proposal_id == proposal.id
        assert snapshot.title == "Test Proposal"
        assert snapshot.status == "pending_review"
        assert snapshot.metadata_payload == {"key": "value"}


def test_list_proposals(session_factory, service):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p1 = Proposal(
            session_id=conv.id,
            title="Prop 1",
            summary="Sum 1",
            status=ProposalStatus.PENDING_REVIEW,
            proposal_type="scout",
        )
        p2 = Proposal(
            session_id=conv.id,
            title="Prop 2",
            summary="Sum 2",
            status=ProposalStatus.REJECTED,
            proposal_type="scout",
        )
        session.add_all([p1, p2])
        session.commit()

    proposals = execution_proposal_service.list_proposals(
        service, status=ProposalStatus.PENDING_REVIEW
    )
    assert len(proposals) == 1
    assert proposals[0].title == "Prop 1"


def test_build_task_text_for_proposal_rich_metadata():
    p = Proposal(
        title="Optimization",
        summary="Speed up queries",
        content="Detailed plan",
        metadata_payload={
            "diff_text": "- old\n+ new",
            "files_changed": ["db/models.py", "repositories/sqlalchemy.py"],
            "json_payload": {"nested": 123},
        },
    )
    text = execution_proposal_service._build_task_text_for_proposal(p)
    assert "Optimization" in text
    assert "Speed up queries" in text
    assert "Details:\nDetailed plan" in text
    assert "Diff:\n```diff\n- old\n+ new\n```" in text
    assert "Files changed:\ndb/models.py\nrepositories/sqlalchemy.py" in text
    assert 'JSON Payload:\n```json\n{\n  "nested": 123\n}\n```' in text


def test_accept_proposal_not_found(service):
    status, snapshot, detail = execution_proposal_service.accept_proposal(service, "non-existent")
    assert status == "not_found"
    assert snapshot is None
    assert "was not found" in detail


def test_accept_proposal_already_accepted_idempotent(session_factory, service):
    now = datetime.now(UTC)
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p = Proposal(
            session_id=conv.id,
            title="Prop",
            summary="Sum",
            status=ProposalStatus.ACCEPTED,
            metadata_payload={"accepted_task_id": "task_123"},
        )
        session.add(p)
        session.commit()
        pid = p.id

    service.tasks["task_123"] = TaskSnapshot(
        task_id="task_123",
        session_id="s1",
        task_text="text",
        status="pending",
        created_at=now,
        updated_at=now,
    )

    status, snapshot, detail = execution_proposal_service.accept_proposal(service, pid)
    assert status == "conflict"
    assert snapshot.task_id == "task_123"
    assert "already accepted" in detail


def test_accept_proposal_already_accepted_missing_task_id(session_factory, service):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p = Proposal(
            session_id=conv.id,
            title="Prop",
            summary="Sum",
            status=ProposalStatus.ACCEPTED,
            metadata_payload={},
        )
        session.add(p)
        session.commit()
        pid = p.id

    status, snapshot, detail = execution_proposal_service.accept_proposal(service, pid)
    assert status == "conflict"
    assert snapshot is None
    assert "missing accepted_task_id" in detail


def test_accept_proposal_invalid_status(session_factory, service):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p = Proposal(
            session_id=conv.id,
            title="Prop",
            summary="Sum",
            status=ProposalStatus.REJECTED,
        )
        session.add(p)
        session.commit()
        pid = p.id

    status, snapshot, detail = execution_proposal_service.accept_proposal(service, pid)
    assert status == "conflict"
    assert snapshot is None
    assert "cannot be accepted from status" in detail


def test_accept_proposal_missing_session_or_user(session_factory, service):
    with session_factory() as session:
        p = Proposal(
            session_id="missing-session",
            title="Prop",
            summary="Sum",
            status=ProposalStatus.PENDING_REVIEW,
        )
        session.add(p)
        session.commit()
        pid = p.id

    status, snapshot, detail = execution_proposal_service.accept_proposal(service, pid)
    assert status == "conflict"
    assert "Session for proposal not found" in detail

    with session_factory() as session:
        conv = ConversationSession(user_id="missing-user", channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p2 = Proposal(
            session_id=conv.id,
            title="Prop2",
            summary="Sum2",
            status=ProposalStatus.PENDING_REVIEW,
        )
        session.add(p2)
        session.commit()
        p2id = p2.id

    status, snapshot, detail = execution_proposal_service.accept_proposal(service, p2id)
    assert status == "conflict"
    assert "User for proposal session not found" in detail


def test_accept_proposal_success_path(session_factory, service):
    with session_factory() as session:
        user = User(external_user_id="u1", display_name="User One")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        src_task = Task(
            session_id=conv.id,
            task_text="src",
            status=TaskStatus.COMPLETED,
            repo_url="http://repo",
            branch="main",
        )
        session.add(src_task)
        session.flush()

        p = Proposal(
            session_id=conv.id,
            task_id=src_task.id,
            title="Prop",
            summary="Sum",
            status=ProposalStatus.PENDING_REVIEW,
        )
        session.add(p)
        session.commit()
        pid = p.id

    status, snapshot, detail = execution_proposal_service.accept_proposal(service, pid)
    assert status == "created"
    assert snapshot is not None
    assert detail is None

    with session_factory() as session:
        p_updated = session.get(Proposal, pid)
        assert p_updated.status == ProposalStatus.ACCEPTED
        assert p_updated.metadata_payload["accepted_task_id"] == snapshot.task_id


def test_reject_proposal(session_factory, service):
    status, snapshot, detail = execution_proposal_service.reject_proposal(service, "missing")
    assert status == "not_found"

    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        conv_id = conv.id
        p = Proposal(
            session_id=conv_id,
            title="Prop",
            summary="Sum",
            status=ProposalStatus.REJECTED,
        )
        session.add(p)
        session.commit()
        pid = p.id

    status, snapshot, detail = execution_proposal_service.reject_proposal(service, pid)
    assert status == "success"
    assert snapshot.proposal_id == pid

    with session_factory() as session:
        p2 = Proposal(
            session_id=conv_id,
            title="Prop2",
            summary="Sum2",
            status=ProposalStatus.ACCEPTED,
        )
        session.add(p2)
        session.commit()
        p2id = p2.id

    status, snapshot, detail = execution_proposal_service.reject_proposal(service, p2id)
    assert status == "conflict"

    with session_factory() as session:
        p3 = Proposal(
            session_id=conv_id,
            title="Prop3",
            summary="Sum3",
            status=ProposalStatus.PENDING_REVIEW,
        )
        session.add(p3)
        session.commit()
        p3id = p3.id

    status, snapshot, detail = execution_proposal_service.reject_proposal(service, p3id)
    assert status == "success"
    assert snapshot.proposal_id == p3id


def test_accept_proposal_concurrent_deletion(session_factory, service):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p = Proposal(
            session_id=conv.id,
            title="Prop",
            summary="Sum",
            status=ProposalStatus.PENDING_REVIEW,
        )
        session.add(p)
        session.commit()
        pid = p.id

    orig_create = service.create_task_outcome

    def mock_create(*args, **kwargs):
        with session_factory() as s:
            prop = s.get(Proposal, pid)
            s.delete(prop)
            s.commit()
        return orig_create(*args, **kwargs)

    service.create_task_outcome = mock_create

    with pytest.raises(ValueError, match="not found"):
        execution_proposal_service.accept_proposal(service, pid)
    assert len(service.canceled_task_ids) == 1


def test_accept_proposal_concurrent_accept_different_task(session_factory, service):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p = Proposal(
            session_id=conv.id,
            title="Prop",
            summary="Sum",
            status=ProposalStatus.PENDING_REVIEW,
        )
        session.add(p)
        session.commit()
        pid = p.id

    orig_create = service.create_task_outcome

    def mock_create(*args, **kwargs):
        with session_factory() as s:
            prop = s.get(Proposal, pid)
            prop.status = ProposalStatus.ACCEPTED
            prop.metadata_payload = {"accepted_task_id": "other_task_999"}
            s.commit()
        return orig_create(*args, **kwargs)

    service.create_task_outcome = mock_create
    now = datetime.now(UTC)
    service.tasks["other_task_999"] = TaskSnapshot(
        task_id="other_task_999",
        session_id="s1",
        task_text="t",
        status="pending",
        created_at=now,
        updated_at=now,
    )

    status, snapshot, detail = execution_proposal_service.accept_proposal(service, pid)
    assert status == "conflict"
    assert snapshot.task_id == "other_task_999"
    assert "different task" in detail
    assert len(service.canceled_task_ids) == 1


def test_accept_proposal_concurrent_rowcount_zero_scenarios(session_factory, service, monkeypatch):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        conv_id = conv.id
        p1 = Proposal(
            session_id=conv_id, title="P1", summary="S1", status=ProposalStatus.PENDING_REVIEW
        )
        p2 = Proposal(
            session_id=conv_id, title="P2", summary="S2", status=ProposalStatus.PENDING_REVIEW
        )
        p3 = Proposal(
            session_id=conv_id, title="P3", summary="S3", status=ProposalStatus.PENDING_REVIEW
        )
        session.add_all([p1, p2, p3])
        session.commit()
        p1_id, p2_id, p3_id = p1.id, p2.id, p3.id

    orig_create = service.create_task_outcome

    # Case 1: proposal status changed to REJECTED during create_task_outcome
    # (triggers rowcount == 0 & refetched status != ACCEPTED)

    def mock_create_reject_status(*args, **kwargs):
        outcome = orig_create(*args, **kwargs)
        with session_factory() as s:
            prop = s.get(Proposal, p1_id)
            prop.status = ProposalStatus.REJECTED
            s.commit()
        return outcome

    service.create_task_outcome = mock_create_reject_status
    status, snapshot, detail = execution_proposal_service.accept_proposal(service, p1_id)
    assert status == "conflict"
    assert "modified concurrently" in detail

    # Case 2: proposal accepted concurrently with SAME task ID
    def mock_create_same(*args, **kwargs):
        outcome = orig_create(*args, **kwargs)
        with session_factory() as s:
            prop = s.get(Proposal, p2_id)
            prop.status = ProposalStatus.ACCEPTED
            prop.metadata_payload = {"accepted_task_id": outcome.task_snapshot.task_id}
            s.commit()
        return outcome

    service.create_task_outcome = mock_create_same
    status, snapshot, detail = execution_proposal_service.accept_proposal(service, p2_id)
    assert status == "conflict"
    assert "was accepted concurrently" in detail

    # Case 3: proposal status changed to something else (e.g. REJECTED) concurrently
    def mock_create_reject(*args, **kwargs):
        outcome = orig_create(*args, **kwargs)
        with session_factory() as s:
            prop = s.get(Proposal, p3_id)
            prop.status = ProposalStatus.REJECTED
            s.commit()
        return outcome

    service.create_task_outcome = mock_create_reject
    status, snapshot, detail = execution_proposal_service.accept_proposal(service, p3_id)
    assert status == "conflict"
    assert "modified concurrently" in detail


def test_reject_proposal_concurrent_rowcount_zero_scenarios(session_factory, service, monkeypatch):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p1 = Proposal(
            session_id=conv.id, title="P1", summary="S1", status=ProposalStatus.PENDING_REVIEW
        )
        p2 = Proposal(
            session_id=conv.id, title="P2", summary="S2", status=ProposalStatus.PENDING_REVIEW
        )
        p3 = Proposal(
            session_id=conv.id, title="P3", summary="S3", status=ProposalStatus.PENDING_REVIEW
        )
        session.add_all([p1, p2, p3])
        session.commit()
        p1_id, p2_id, p3_id = p1.id, p2.id, p3.id

    # Mock execute for p1: rowcount = 0, refetched is None (deleted)
    with session_factory() as s:
        s.delete(s.get(Proposal, p1_id))
        s.commit()

    status, snapshot, detail = execution_proposal_service.reject_proposal(service, p1_id)
    assert status == "not_found"

    # Mock execute for p2: status changed to REJECTED concurrently
    with session_factory() as s:
        p2_obj = s.get(Proposal, p2_id)
        p2_obj.status = ProposalStatus.REJECTED
        s.commit()

    status, snapshot, detail = execution_proposal_service.reject_proposal(service, p2_id)
    assert status == "success"

    # Mock execute for p3: status changed to ACCEPTED concurrently
    with session_factory() as s:
        p3_obj = s.get(Proposal, p3_id)
        p3_obj.status = ProposalStatus.ACCEPTED
        s.commit()


def test_accept_proposal_source_task_missing(session_factory, service):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p = Proposal(
            session_id=conv.id,
            task_id="nonexistent-source-task",
            title="Prop",
            summary="Sum",
            status=ProposalStatus.PENDING_REVIEW,
        )
        session.add(p)
        session.commit()
        pid = p.id

    status, snapshot, detail = execution_proposal_service.accept_proposal(service, pid)
    assert status == "created"
    assert snapshot is not None


def test_accept_proposal_refetched_none_after_update(session_factory, service, monkeypatch):
    with session_factory() as session:
        user = User(external_user_id="u1")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="t1")
        session.add(conv)
        session.flush()
        p = Proposal(
            session_id=conv.id,
            title="Prop",
            summary="Sum",
            status=ProposalStatus.PENDING_REVIEW,
        )
        session.add(p)
        session.commit()
        pid = p.id

    _orig_execute = session_factory().execute

    # We mock execute to return rowcount 0, and mock session.get(Proposal, pid) to return None
    class FakeResult:
        rowcount = 0

    _orig_get = sessionmaker.object_session if hasattr(sessionmaker, "object_session") else None

    # Patch session.execute and session.get for this accept call
    def mock_accept_with_deleted_refetch(self_service, proposal_id):
        with execution_proposal_service.session_scope(self_service.session_factory) as session:
            repo = execution_proposal_service.ProposalRepository(session)
            proposal = repo.get_proposal(proposal_id)
            session_repo = execution_proposal_service.SessionRepository(session)
            user_repo = execution_proposal_service.UserRepository(session)
            _task_repo = execution_proposal_service.TaskRepository(session)

            conversation_session = session_repo.get(proposal.session_id)
            user = user_repo.get(conversation_session.user_id)

            task_text = execution_proposal_service._build_task_text_for_proposal(proposal)
            channel = conversation_session.channel
            external_thread_id = conversation_session.external_thread_id
            external_user_id = user.external_user_id or "unknown"
            display_name = user.display_name

        submission = TaskSubmission(
            task_text=task_text,
            repo_url=None,
            branch=None,
            priority=0,
            session=execution_proposal_service.SubmissionSession(
                channel=channel,
                external_user_id=external_user_id,
                external_thread_id=external_thread_id,
                display_name=display_name,
            ),
        )
        delivery_key = execution_proposal_service.DeliveryKey(
            channel=channel,
            delivery_id=f"proposal_{proposal_id}",
        )
        outcome = self_service.create_task_outcome(submission, delivery_key=delivery_key)

        with execution_proposal_service.session_scope(self_service.session_factory) as session:
            # Delete proposal so session.get(Proposal, proposal_id) returns None
            prop = session.get(Proposal, proposal_id)
            session.delete(prop)
            session.commit()

        # Simulate update returning rowcount=0 and refetched returning None
        with execution_proposal_service.session_scope(self_service.session_factory) as session:
            refetched = session.get(Proposal, proposal_id)
            if refetched is None:
                if not outcome.duplicate:
                    self_service.cancel_task(task_id=outcome.task_snapshot.task_id)
                return "not_found", None, f"Proposal '{proposal_id}' was deleted concurrently."

    res_status, res_snap, res_detail = mock_accept_with_deleted_refetch(service, pid)
    assert res_status == "not_found"
    assert "deleted concurrently" in res_detail
    assert len(service.canceled_task_ids) == 1
