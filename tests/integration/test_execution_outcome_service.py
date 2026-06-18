"""Integration tests for execution outcome service, including Proposal creation for Scout mode."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import ProposalStatus, ProposalType
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.reflection import FrictionReport
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


def _assert_sandbox_improvement_proposal(proposal: Any, *, task_id: str) -> None:
    metadata = proposal.metadata_payload
    suggestion = metadata["improvement_suggestion"]
    friction_report = metadata["friction_report"]

    assert proposal.status == ProposalStatus.PENDING_REVIEW
    assert proposal.title == "Harden sandbox infrastructure recovery"
    assert proposal.summary == suggestion["description"]
    assert metadata["reflection_kind"] == "improvement_suggestion"
    assert friction_report["description"] == "Infra crash prevented checkout."
    assert friction_report["task_id"] == task_id
    assert friction_report["worker_run_id"]
    assert metadata["failure_kind"] == "sandbox_infra"
    assert metadata["fingerprint"]
    assert suggestion["value"] == "high"
    assert suggestion["effort"] == "large"
    assert suggestion["risk"] == "high"
    assert suggestion["layer_impact"] == "sandbox"
    assert suggestion["hitl_need"] == "required"


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


def test_persist_execution_outcome_creates_scored_improvement_proposal(session_factory) -> None:
    """Repeated friction should produce an idempotent scored reflection proposal."""
    service = MockExecutionService(session_factory)

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = OrchestratorState(
        task=TaskRequest(task_text="fix sandbox", task_id=task_id),
        session=None,
        route=RouteDecision(chosen_worker="codex", route_reason="retry after infra failure"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal="fix sandbox",
            task_type="maintenance",
            delivery_mode="workspace",
        ),
        result=WorkerResult(
            status="failure",
            summary="Sandbox infra crash blocked execution.",
            failure_kind="sandbox_infra",
            commands_run=[],
            test_results=[],
            artifacts=[],
        ),
        friction_reports=[
            FrictionReport(
                task_id=task_id,
                source="sandbox",
                description="Infra crash prevented checkout.",
                impact="blocked",
                context={"failure_kind": "sandbox_infra"},
            )
        ],
        attempt_count=2,
    )
    now = datetime.now(UTC)

    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert len(proposals) == 1
        _assert_sandbox_improvement_proposal(proposals[0], task_id=task_id)

    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert len(proposals) == 1


def test_persist_execution_outcome_scores_worker_friction_report_dict(session_factory) -> None:
    """Worker-emitted friction dicts should be parsed and persisted as suggestions."""
    service = MockExecutionService(session_factory)

    with session_scope(session_factory) as session:
        task_id = _setup_task(session)

    state = OrchestratorState(
        task=TaskRequest(task_text="complete after workaround", task_id=task_id),
        session=None,
        route=RouteDecision(chosen_worker="gemini", route_reason="retry same worker"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal="complete after workaround",
            task_type="maintenance",
            delivery_mode="workspace",
        ),
        result=WorkerResult(
            status="success",
            summary="Completed after workaround.",
            commands_run=[],
            test_results=[],
            artifacts=[],
            friction_reports=[
                {
                    "source": "instructions",
                    "description": "Instructions forced an unnecessary workaround.",
                    "impact": "required_workaround",
                    "context": {"origin": "worker"},
                }
            ],
        ),
        attempt_count=2,
    )
    now = datetime.now(UTC)

    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
    )

    with session_scope(session_factory) as session:
        proposals = ProposalRepository(session).list_proposals(
            task_id=task_id,
            proposal_type=ProposalType.REFLECTION,
        )
        assert len(proposals) == 1
        metadata = proposals[0].metadata_payload
        suggestion = metadata["improvement_suggestion"]

        assert metadata["reflection_kind"] == "improvement_suggestion"
        assert metadata["friction_report"]["context"] == {"origin": "worker"}
        assert suggestion["value"] == "high"
        assert suggestion["effort"] == "small"
        assert suggestion["risk"] == "low"
        assert suggestion["layer_impact"] == "worker"
        assert suggestion["hitl_need"] == "none"
