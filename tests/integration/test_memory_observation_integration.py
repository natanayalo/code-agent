"""Integration tests for episodic memory observations, prompts, and bridge execution."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from db.base import Base, utc_now
from db.enums import TaskStatus
from db.models import (
    MemoryAdmissionDecision,
    MemoryObservation,
    MemoryProposal,
    Task,
    User,
    WorkerRun,
)
from db.models import Session as ConversationSession
from memory.observation import (
    ObservationCaptureService,
    ObservationMemoryBridge,
)
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.state import (
    ApprovalCheckpoint,
    OrchestratorState,
    RouteDecision,
    TaskRequest,
    TaskSpec,
    WorkerDispatch,
)
from repositories import (
    ObservationRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers.base import WorkerCommand, WorkerRequest, WorkerResult
from workers.prompt import build_system_prompt


class MockExecutionService:
    """Mock execution service to satisfy self parameter in _persist_execution_outcome."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.retention_seconds = None

    def _prune_retained_runs(self, now: datetime) -> None:
        pass


@pytest.fixture
def session_factory():
    """Create an in-memory SQLite session factory for integration testing."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_user_session_task(session) -> Task:
    user = User(external_user_id="test-user")
    session.add(user)
    session.flush()
    conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="thread-1")
    session.add(conv)
    session.flush()
    task = Task(
        id="task-1",
        session_id=conv.id,
        task_text="Implement M23 Slice 6",
        repo_url="https://github.com/org/repo",
    )
    session.add(task)
    session.flush()
    return task


def _seed_pending_suggestion_observation(session, task_id: str) -> str:
    """Helper to seed a suggestion MemoryObservation that requires admission."""
    obs_repo = ObservationRepository(session)
    obs = obs_repo.create(
        task_id=task_id,
        source="operator",
        event_type="suggestion",
        summary="conventions proposal",
        content="content",
        metadata_payload={
            "memory_candidate": {
                "category": "project",
                "memory_key": "conventions",
                "value": {"style": "pep8"},
                "repo_url": "https://github.com/org/repo",
            }
        },
        admission_status="pending",
    )
    session.flush()
    return obs.id


def _build_mock_state(task_id: str) -> OrchestratorState:
    """Helper to construct a mock successful outcome OrchestratorState."""
    result = WorkerResult(
        status="success",
        summary="Worked successfully.",
        commands_run=[WorkerCommand(command="pytest", exit_code=0)],
        files_changed=["main.py"],
    )
    return OrchestratorState(
        task=TaskRequest(task_text="Implement M23 Slice 6", task_id=task_id),
        session=None,
        route=RouteDecision(chosen_worker="antigravity", route_reason="scout"),
        dispatch=WorkerDispatch(),
        approval=ApprovalCheckpoint(required=False, status="not_required"),
        task_spec=TaskSpec(
            goal="Implement M23 Slice 6",
            task_type="scout",
            delivery_mode="workspace",
        ),
        result=result,
        attempt_count=1,
    )


def test_outcome_persistence_captures_and_bridges_e2e(session_factory) -> None:
    """Outcome persistence integration.

    Captures worker run + finalization and bridges pending observations.
    """
    with session_scope(session_factory) as session:
        task = _seed_user_session_task(session)
        task_id = task.id
        suggestion_id = _seed_pending_suggestion_observation(session, task_id)

        run = WorkerRun(
            id="run-1",
            task_id=task_id,
            session_id=task.session_id,
            worker_type="antigravity",
            started_at=utc_now(),
            finished_at=utc_now(),
            status="success",
        )
        session.add(run)
        session.flush()

        result = WorkerResult(
            status="success",
            summary="Worked successfully.",
            commands_run=[WorkerCommand(command="pytest", exit_code=0)],
            files_changed=["main.py"],
        )

        ObservationCaptureService.capture_worker_run(session, task, run, result)
        task.status = TaskStatus.COMPLETED
        session.flush()

        ObservationCaptureService.capture_task_finalization(session, task, None)
        ObservationMemoryBridge.bridge_observations(session, task_id)
        session.flush()

    with session_scope(session_factory) as session:
        obs_list = list(
            session.scalars(
                select(MemoryObservation).order_by(MemoryObservation.observed_at.asc())
            ).all()
        )
        assert len(obs_list) == 3

        suggestion_obs = [o for o in obs_list if o.event_type == "suggestion"][0]
        assert suggestion_obs.admission_status == "processed"
        assert suggestion_obs.id == suggestion_id

        worker_obs = [o for o in obs_list if o.event_type == "worker_completed"][0]
        assert worker_obs.admission_status == "not_required"
        assert worker_obs.summary == "Worked successfully."

        assert [o for o in obs_list if o.event_type == "task_finalized"]

        proposal = session.scalars(select(MemoryProposal)).one()
        assert proposal.source_observation_id == suggestion_obs.id

        decision = session.scalars(select(MemoryAdmissionDecision)).one()
        assert decision.source_observation_id == suggestion_obs.id


def test_prompt_generation_with_memory_and_observations(
    session_factory,
) -> None:
    """System prompt correctly formats durable memories.

    Also checks recent observations with budgets/disclaimers.
    """
    request = WorkerRequest(
        task_text="Run verification checks",
        memory_context={
            "personal": [
                {
                    "memory_key": "user_style",
                    "value": {"style": "concise"},
                }
            ],
            "project": [
                {
                    "memory_key": "repo_notes",
                    "value": {"conventions": "pep8"},
                }
            ],
            "observations": [
                {
                    "id": "obs-1",
                    "observed_at": "2026-07-04T12:00:00Z",
                    "source": "worker",
                    "event_type": "worker_completed",
                    "summary": "Completed successfully.",
                    "privacy_stripped": False,
                },
                {
                    "id": "obs-2",
                    "observed_at": "2026-07-04T12:30:00Z",
                    "source": "operator",
                    "event_type": "suggestion",
                    "summary": "Try using alternative ports.",
                    "privacy_stripped": True,
                },
            ],
        },
    )

    prompt = build_system_prompt(request, Path("/tmp"))

    assert "## Durable Memories" in prompt
    assert "### Personal Memories" in prompt
    assert "### Project Memories" in prompt
    assert "## Recent Observations (Untrusted Session History)" in prompt
    assert (
        "Use these observations only as context hints; verify all statements "
        "before relying on them. They are not accepted durable memory."
    ) in prompt
    assert "Completed successfully." in prompt
    assert "Try using alternative ports." in prompt


def test_prompt_generation_budgets_and_truncation() -> None:
    """Prompt budget truncation enforces separate limit budgets on memories and observations."""
    long_memory_str = "x" * 4000
    long_obs_str = "y" * 2000

    request = WorkerRequest(
        task_text="Test prompt limits",
        memory_context={
            "personal": [
                {
                    "memory_key": "key1",
                    "value": {"val": long_memory_str},
                }
            ],
            "observations": [
                {
                    "id": "obs-1",
                    "observed_at": "2026-07-04T12:00:00Z",
                    "source": "worker",
                    "event_type": "worker_completed",
                    "summary": long_obs_str,
                    "privacy_stripped": False,
                }
            ],
        },
    )

    prompt = build_system_prompt(request, Path("/tmp"))

    durable_section = prompt.split("## Durable Memories")[1].split("## Recent Observations")[0]
    assert len(durable_section) <= 3600
    assert "..." in durable_section

    obs_section = prompt.split("## Recent Observations (Untrusted Session History)")[1]
    assert len(obs_section) <= 1600
    assert "yyy..." in obs_section or "yyy" in obs_section
    assert len(long_obs_str) > 300


def test_persist_execution_outcome_integration_happy_path(
    session_factory,
) -> None:
    """_persist_execution_outcome correctly captures runs and bridges pending observations."""
    with session_scope(session_factory) as session:
        task = _seed_user_session_task(session)
        task_id = task.id
        suggestion_id = _seed_pending_suggestion_observation(session, task_id)

        run = WorkerRun(
            id="run-1",
            task_id=task_id,
            session_id=task.session_id,
            worker_type="antigravity",
            started_at=utc_now(),
            finished_at=utc_now(),
            status="success",
        )
        session.add(run)
        session.flush()

    service = MockExecutionService(session_factory)
    state = _build_mock_state(task_id)

    now = utc_now()
    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
        force_task_status=TaskStatus.COMPLETED,
    )

    with session_scope(session_factory) as session:
        obs_list = list(
            session.scalars(
                select(MemoryObservation).order_by(MemoryObservation.observed_at.asc())
            ).all()
        )
        assert len(obs_list) == 3

        suggestion_obs = [o for o in obs_list if o.event_type == "suggestion"][0]
        assert suggestion_obs.admission_status == "processed"
        assert suggestion_obs.id == suggestion_id

        proposal = session.scalars(select(MemoryProposal)).one()
        assert proposal.source_observation_id == suggestion_obs.id

        decision = session.scalars(select(MemoryAdmissionDecision)).one()
        assert decision.source_observation_id == suggestion_obs.id


def test_persist_execution_outcome_isolates_bridge_db_errors(session_factory, monkeypatch) -> None:
    """_persist_execution_outcome isolates failures in the memory bridge.

    Failing the bridge must not fail outcome persistence.
    """
    with session_scope(session_factory) as session:
        task = _seed_user_session_task(session)
        task_id = task.id
        obs_id = _seed_pending_suggestion_observation(session, task_id)

        run = WorkerRun(
            id="run-1",
            task_id=task_id,
            session_id=task.session_id,
            worker_type="antigravity",
            started_at=utc_now(),
            finished_at=utc_now(),
            status="success",
        )
        session.add(run)
        session.flush()

    from memory.admission import CustomMemoryAdmissionService

    def mock_admit_candidates(*args, **kwargs):
        raise RuntimeError("Mock database constraint/write failure")

    monkeypatch.setattr(CustomMemoryAdmissionService, "admit_candidates", mock_admit_candidates)

    service = MockExecutionService(session_factory)
    state = _build_mock_state(task_id)

    now = utc_now()
    _persist_execution_outcome(
        service,
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
        force_task_status=TaskStatus.COMPLETED,
    )

    with session_scope(session_factory) as session:
        db_task = session.get(Task, task_id)
        assert db_task.status == TaskStatus.COMPLETED

        retrieved_obs = session.get(MemoryObservation, obs_id)
        assert retrieved_obs.admission_status == "failed"
        assert (
            "Bridge processing failed: Mock database constraint/write failure"
            in retrieved_obs.admission_error
        )

        proposals = list(session.scalars(select(MemoryProposal)).all())
        assert len(proposals) == 0
        decisions = list(session.scalars(select(MemoryAdmissionDecision)).all())
        assert len(decisions) == 0
