"""Integration tests for execution outcome service, including Proposal creation for Scout mode."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import ProposalStatus
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.state import (
    ApprovalCheckpoint,
    OrchestratorState,
    RouteDecision,
    TaskRequest,
    TaskSpec,
    WorkerDispatch,
    WorkerResult,
)
from repositories import (
    ProposalRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


class MockExecutionService:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.retention_seconds = None

    def _prune_retained_runs(self, now: datetime) -> None:
        pass


@pytest.fixture
def session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _setup_task(session) -> str:
    user = UserRepository(session).create(
        external_user_id="test-scout",
        display_name="Test Scout",
    )
    sess = SessionRepository(session).create(
        user_id=user.id,
        channel="http",
        external_thread_id="thread-test",
    )
    task = TaskRepository(session).create(
        session_id=sess.id,
        task_text="Do scout stuff",
    )
    return task.id


def test_persist_execution_outcome_creates_proposal_for_scout(session_factory) -> None:
    """Successful scout tasks should produce an idempotent Proposal in the Idea Inbox."""
    service = MockExecutionService(session_factory)

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)
        task = TaskRepository(session).get(task_id)
        assert task is not None
        task_session_id = task.session_id

    state = OrchestratorState(
        task=TaskRequest(task_text="scout this", task_id=task_id),
        session=None,
        route=RouteDecision(chosen_worker="codex", route_reason="scout route"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal="scout this",
            task_type="scout",
            delivery_mode="summary",
        ),
        result=WorkerResult(
            status="success",
            summary="Found some interesting files.",
            files_changed=["a.txt"],
            commands_run=[],
            test_results=[],
            artifacts=[],
            budget_usage={"cost": 1.0},
            diff_text="diff content",
        ),
    )

    now = datetime.now(UTC)

    # First persistence call should create the proposal
    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(task_id=task_id)
        assert len(proposals) == 1
        assert proposals[0].session_id == task_session_id
        assert proposals[0].status == ProposalStatus.PENDING_REVIEW
        assert proposals[0].summary == "Found some interesting files."
        assert proposals[0].metadata_payload["source"] == "scout"
        assert proposals[0].metadata_payload["files_changed"] == ["a.txt"]
        assert proposals[0].metadata_payload["budget_usage"] == {"cost": 1.0}
        assert proposals[0].metadata_payload["diff_text"] == "diff content"

    # Second persistence call with same task_id should be idempotent
    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(task_id=task_id)
        assert len(proposals) == 1  # Still 1, idempotency works
