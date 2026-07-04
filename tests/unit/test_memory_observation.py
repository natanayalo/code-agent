"""Unit tests for memory observation capture, tag stripping, and bridging."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import HumanInteractionStatus, HumanInteractionType, TaskStatus
from db.models import (
    HumanInteraction,
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
    ObservationContextService,
    ObservationMemoryBridge,
    strip_private_tags,
    strip_private_tags_recursive,
)
from repositories import (
    ObservationRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers.base import WorkerCommand, WorkerResult


@pytest.fixture
def session_factory():
    """Create an in-memory SQLite session factory for testing."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_task(session) -> Task:
    """Helper to seed user, session, and task to satisfy foreign keys and NOT NULL constraints."""
    user = User(external_user_id="test-user")
    session.add(user)
    session.flush()
    conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="thread-1")
    session.add(conv)
    session.flush()
    task = Task(id="task-1", session_id=conv.id, task_text="Implement code", repo_url="repo1")
    session.add(task)
    session.flush()
    return task


def test_strip_private_tags() -> None:
    """We can strip <private> blocks from free text."""
    text = "Public part. <private>Private message</private> More public."
    redacted, stripped = strip_private_tags(text)
    assert redacted == "Public part. [redacted-private] More public."
    assert stripped is True

    # Case insensitive
    text_caps = "Start <PRIVATE>secret</PRIVATE> End"
    redacted_caps, stripped_caps = strip_private_tags(text_caps)
    assert redacted_caps == "Start [redacted-private] End"
    assert stripped_caps is True

    # No match
    text_no = "Nothing to hide."
    redacted_no, stripped_no = strip_private_tags(text_no)
    assert redacted_no == "Nothing to hide."
    assert stripped_no is False


def test_strip_private_tags_recursive() -> None:
    """We recursively strip private tags from string values in dictionaries and lists."""
    payload = {
        "summary": "This is <private>secret summary</private>",
        "nested": {
            "key": "value <private>here</private>",
            "number": 123,
        },
        "items": [
            "Normal item",
            "<private>Secret item</private>",
        ],
    }
    redacted, stripped = strip_private_tags_recursive(payload)
    assert stripped is True
    assert redacted["summary"] == "This is [redacted-private]"
    assert redacted["nested"]["key"] == "value [redacted-private]"
    assert redacted["nested"]["number"] == 123
    assert redacted["items"] == ["Normal item", "[redacted-private]"]


def test_capture_worker_run(session_factory) -> None:
    """capture_worker_run captures run outcomes.

    Sets admission_status='not_required'.
    """
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        run = WorkerRun(
            id="run-1",
            task_id=task.id,
            session_id=task.session_id,
            worker_type="antigravity",
            started_at=datetime.now(),
            finished_at=datetime.now(),
            status="success",
        )
        session.add(run)
        session.flush()

        result = WorkerResult(
            status="success",
            summary="Done building <private>secret module</private>.",
            commands_run=[WorkerCommand(command="pytest", exit_code=0)],
            files_changed=["app.py"],
        )

        obs = ObservationCaptureService.capture_worker_run(session, task, run, result)
        session.flush()
        obs_id = obs.id

    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        retrieved = repo.get(obs_id)
        assert retrieved is not None
        assert retrieved.source == "worker"
        assert retrieved.event_type == "worker_completed"
        assert retrieved.summary == "Done building [redacted-private]."
        assert retrieved.admission_status == "not_required"
        assert retrieved.privacy_stripped is True
        assert retrieved.metadata_payload["files_changed"] == ["app.py"]


def test_capture_task_finalization(session_factory) -> None:
    """capture_task_finalization logs task completion."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task.status = TaskStatus.COMPLETED
        session.flush()

        obs = ObservationCaptureService.capture_task_finalization(session, task, None)
        session.flush()
        obs_id = obs.id

    with session_scope(session_factory) as session:
        retrieved = ObservationRepository(session).get(obs_id)
        assert retrieved is not None
        assert retrieved.source == "orchestrator"
        assert retrieved.event_type == "task_finalized"
        assert retrieved.admission_status == "not_required"


def test_capture_interaction_resolution(session_factory) -> None:
    """capture_interaction_resolution records resolved human interactions."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        interaction = HumanInteraction(
            id="int-1",
            task_id=task.id,
            interaction_type=HumanInteractionType.PERMISSION,
            status=HumanInteractionStatus.RESOLVED,
            summary="Approved tool usage",
            response_data={"approved": True},
        )
        session.add(interaction)
        session.flush()

        obs = ObservationCaptureService.capture_interaction_resolution(session, task, interaction)
        session.flush()
        obs_id = obs.id

    with session_scope(session_factory) as session:
        retrieved = ObservationRepository(session).get(obs_id)
        assert retrieved is not None
        assert retrieved.source == "operator"
        assert retrieved.event_type == "interaction_resolved"
        assert retrieved.metadata_payload["response_data"] == {"approved": True}


def test_build_recent_context_block(session_factory) -> None:
    """build_recent_context_block returns DTOs for recent observations."""
    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        repo.create(
            source="worker",
            event_type="test",
            summary="obs 1",
            content="content 1",
            repo_url="repo1",
        )
        repo.create(
            source="worker",
            event_type="test",
            summary="obs 2",
            content="content 2",
            repo_url="repo1",
        )
        session.flush()

    with session_scope(session_factory) as session:
        entries = ObservationContextService.build_recent_context_block(session, repo_url="repo1")
        assert len(entries) == 2
        assert {e.summary for e in entries} == {"obs 1", "obs 2"}


def test_bridge_observations_success_and_idempotency(session_factory) -> None:
    """The bridge successfully admits candidates, handles idempotency, and updates status."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task_id = task.id

        obs_repo = ObservationRepository(session)
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="operator",
            event_type="suggestion",
            summary="Suggest memory",
            content="content",
            metadata_payload={
                "memory_candidate": {
                    "category": "project",
                    "memory_key": "conventions",
                    "value": {"style": "pep8"},
                }
            },
            admission_status="pending",
        )
        session.flush()

    # Run the bridge
    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    # Verify candidate is admitted as proposal/decision and status updated
    with session_scope(session_factory) as session:
        obs = session.scalars(select(MemoryObservation)).one()
        assert obs.admission_status == "processed"
        assert obs.admission_processed_at is not None

        # Verify proposal was created
        proposal = session.scalars(select(MemoryProposal)).one()
        assert proposal.memory_key == "conventions"
        assert proposal.task_id == task_id
        assert proposal.session_id == task.session_id
        assert proposal.repo_url == task.repo_url
        assert proposal.source_observation_id == obs.id

        # Verify decision was created
        decision = session.scalars(select(MemoryAdmissionDecision)).one()
        assert decision.memory_key == "conventions"
        assert decision.task_id == task_id
        assert decision.session_id == task.session_id
        assert decision.source_observation_id == obs.id

        # 2. Idempotency check: run again.
        # It should skip gracefully because decision already exists.
        obs.admission_status = "pending"
        session.flush()

        ObservationMemoryBridge.bridge_observations(session, task_id)
        assert obs.admission_status == "processed"


def test_bridge_observations_invalid_schema(session_factory) -> None:
    """The bridge marks invalid/missing schemas as 'invalid'."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task_id = task.id

        obs_repo = ObservationRepository(session)
        obs_repo.create(
            task_id=task_id,
            source="operator",
            event_type="suggestion",
            summary="Suggest invalid memory",
            content="content",
            metadata_payload={
                "memory_candidate": {
                    "value": {"style": "pep8"},
                }
            },
            admission_status="pending",
        )
        session.flush()

    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    with session_scope(session_factory) as session:
        obs = session.scalars(select(MemoryObservation)).one()
        assert obs.admission_status == "invalid"
        assert "Validation failed" in obs.admission_error
