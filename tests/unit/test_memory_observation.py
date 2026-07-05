"""Unit tests for memory observation capture, tag stripping, and bridging."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

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
        "sets": {
            "Normal set item",
            "<private>Secret set item</private>",
        },
        "frozen": frozenset({"<private>Secret frozen item</private>"}),
        "<private>secret key</private>": "key value",
    }
    redacted, stripped = strip_private_tags_recursive(payload)
    assert stripped is True
    assert redacted["summary"] == "This is [redacted-private]"
    assert redacted["nested"]["key"] == "value [redacted-private]"
    assert redacted["nested"]["number"] == 123
    assert redacted["items"] == ["Normal item", "[redacted-private]"]
    assert redacted["sets"] == {"Normal set item", "[redacted-private]"}
    assert redacted["frozen"] == frozenset({"[redacted-private]"})
    assert redacted["[redacted-private]"] == "key value"


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


def test_capture_task_finalization_accepts_raw_string_status(session_factory) -> None:
    """Task finalization serialization accepts raw string status values."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task_like = SimpleNamespace(
            id=task.id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            status="completed",
            task_text=task.task_text,
        )

        obs = ObservationCaptureService.capture_task_finalization(session, task_like, None)
        session.flush()
        obs_id = obs.id

    with session_scope(session_factory) as session:
        retrieved = ObservationRepository(session).get(obs_id)
        assert retrieved is not None
        assert retrieved.summary == "Task finalized with status completed."
        assert "Final status in DB: completed" in retrieved.content
        assert retrieved.metadata_payload["final_status"] == "completed"


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


def test_capture_interaction_resolution_accepts_raw_string_status(session_factory) -> None:
    """Interaction resolution serialization accepts raw string status values."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        interaction = SimpleNamespace(
            id="int-raw",
            interaction_type="permission",
            status="resolved",
            summary="Approved tool usage",
            response_data={"approved": True},
        )

        obs = ObservationCaptureService.capture_interaction_resolution(session, task, interaction)
        session.flush()
        obs_id = obs.id

    with session_scope(session_factory) as session:
        retrieved = ObservationRepository(session).get(obs_id)
        assert retrieved is not None
        assert "Resolution Status: resolved" in retrieved.content
        assert retrieved.metadata_payload["interaction_type"] == "permission"


def test_load_recent_context_entries(session_factory) -> None:
    """load_recent_context_entries returns DTOs for recent observations."""
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
        entries = ObservationContextService.load_recent_context_entries(session, repo_url="repo1")
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


def test_trace_extraction_verification_rules(session_factory) -> None:
    """Test deterministic extraction of verification commands."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task_id = task.id

        obs_repo = ObservationRepository(session)
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker completed.",
            content="Trace:",
            metadata_payload={
                "commands_run": [
                    {"command": "pytest tests/unit", "exit_code": 0},
                    {"command": "poetry run python test_script.py", "exit_code": 0},
                ]
            },
            admission_status="not_required",
        )
        session.flush()

    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    with session_scope(session_factory) as session:
        children = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate"
                )
            ).all()
        )
        assert len(children) == 2
        ver_cmd_strings = {
            c.metadata_payload["memory_candidate"]["value"]["command"] for c in children
        }
        assert "pytest tests/unit" in ver_cmd_strings
        assert "poetry run python test_script.py" in ver_cmd_strings
        assert children[0].admission_status == "processed"


def test_trace_extraction_pitfall_rules(session_factory) -> None:
    """Test deterministic extraction of pitfalls."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task_id = task.id

        obs_repo = ObservationRepository(session)
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker completed.",
            content="Trace:",
            metadata_payload={
                "commands_run": [
                    {"command": "python test_script.py", "exit_code": 1},
                    {"command": "poetry run python test_script.py", "exit_code": 0},
                ]
            },
            admission_status="not_required",
        )
        session.flush()

    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    with session_scope(session_factory) as session:
        children = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate"
                )
            ).all()
        )
        # We expect:
        # - 1 verification command: poetry run python test_script.py (exit code 0)
        # - 1 pitfall
        assert len(children) == 2
        pitfalls = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "known_pitfalls"
        ]
        assert len(pitfalls) == 1
        cand_val = pitfalls[0].metadata_payload["memory_candidate"]["value"]
        assert cand_val["failed_command"] == "python test_script.py"
        assert cand_val["corrected_command"] == "poetry run python test_script.py"
        assert pitfalls[0].admission_status == "processed"


def test_trace_extraction_convention_rules(session_factory) -> None:
    """Test deterministic extraction of conventions."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task_id = task.id

        obs_repo = ObservationRepository(session)
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker completed successfully. convention: always run unit tests.",
            content="Trace:",
            metadata_payload={"commands_run": []},
            admission_status="not_required",
        )
        session.flush()

    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    with session_scope(session_factory) as session:
        children = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate"
                )
            ).all()
        )
        assert len(children) == 1
        assert children[0].metadata_payload["memory_candidate"]["value"] == {
            "convention": "always run unit tests."
        }
        assert children[0].admission_status == "processed"
        proposal = session.scalars(
            select(MemoryProposal).where(MemoryProposal.memory_key == "repo_convention")
        ).one()
        assert proposal.source_observation_id == children[0].id


def test_trace_extraction_remember_instruction_rules(session_factory) -> None:
    """Test deterministic extraction of remember instructions from task text."""
    with session_scope(session_factory) as session:
        # Seed task with task text containing "remember to"
        user = User(external_user_id="test-user")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="thread-2")
        session.add(conv)
        session.flush()
        task = Task(
            id="task-2",
            session_id=conv.id,
            task_text="Remember to use python 3.12 always.",
            repo_url="repo2",
        )
        session.add(task)
        session.flush()
        task_id = task.id

        # Seed interaction resolved observation with "always use..."
        obs_repo = ObservationRepository(session)
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="operator",
            event_type="interaction_resolved",
            summary="Interaction resolved. Always use ruff for linting.",
            content="Content",
            admission_status="not_required",
        )
        session.flush()

    # Run bridge to trigger extraction
    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    # Verify that child observations for remember instructions were created
    with session_scope(session_factory) as session:
        children = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate"
                )
            ).all()
        )
        # We expect:
        # - 1 from task_text ("Remember to use python 3.12 always.")
        # - 1 from interaction resolution ("Always use ruff for linting.")
        assert len(children) == 2

        instructs = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "remembered_instruction"
        ]
        assert len(instructs) == 2
        instructions_text = {
            i.metadata_payload["memory_candidate"]["value"]["instruction"] for i in instructs
        }
        assert "Remember to use python 3.12 always." in instructions_text
        assert "Always use ruff for linting." in instructions_text

        # Since it forces human review via _HUMAN_REVIEW_KEYWORDS, check proposals
        proposals = list(
            session.scalars(
                select(MemoryProposal).where(MemoryProposal.memory_key == "remembered_instruction")
            ).all()
        )
        assert len(proposals) == 2
        assert {p.value["instruction"] for p in proposals} == {
            "Remember to use python 3.12 always.",
            "Always use ruff for linting.",
        }


def test_extraction_idempotency(session_factory) -> None:
    """Test that trace extraction is fully idempotent and doesn't duplicate candidates."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task_id = task.id

        obs_repo = ObservationRepository(session)
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker completed.",
            content="Content",
            metadata_payload={
                "commands_run": [
                    {"command": "pytest", "exit_code": 0},
                ]
            },
            admission_status="not_required",
        )
        session.flush()

    # Run bridge the first time
    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    # Check that 1 child observation is created
    with session_scope(session_factory) as session:
        children_first = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate"
                )
            ).all()
        )
        assert len(children_first) == 1
        assert children_first[0].admission_status == "processed"

    # Run bridge a second time
    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    # Check that still only 1 child observation exists (no duplicates created)
    with session_scope(session_factory) as session:
        children_second = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate"
                )
            ).all()
        )
        assert len(children_second) == 1


def test_trace_extraction_custom_expected_verification_commands(session_factory) -> None:
    """Test custom verification commands from task spec/constraints are successfully extracted."""
    with session_scope(session_factory) as session:
        user = User(external_user_id="test-user")
        session.add(user)
        session.flush()
        conv = ConversationSession(user_id=user.id, channel="test", external_thread_id="thread-e2e")
        session.add(conv)
        session.flush()
        task = Task(
            id="task-custom-e2e",
            session_id=conv.id,
            task_text="Run normal workflow.",
            repo_url="repo-custom",
            task_spec={"verification_commands": ["make build", "make ci"]},
            constraints={"verification_commands": ["make deploy-check"]},
        )
        session.add(task)
        session.flush()
        task_id = task.id

        obs_repo = ObservationRepository(session)
        # Seed worker completed with a matching verification command
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker finished.",
            content="Logs trace",
            metadata_payload={
                "commands_run": [
                    {"command": "make build ", "exit_code": 0},
                    {"command": "make deploy-check", "exit_code": 0},
                    {"command": "make non-existing", "exit_code": 0},
                ]
            },
            admission_status="not_required",
        )
        session.flush()

    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    with session_scope(session_factory) as session:
        children = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate"
                )
            ).all()
        )
        # We expect two children (make build and make deploy-check)
        assert len(children) == 2
        cand_vals = {c.metadata_payload["memory_candidate"]["value"]["command"] for c in children}
        assert "make build " in cand_vals
        assert "make deploy-check" in cand_vals

        # Freshly verified commands should set requires_verification=False and last_verified_at
        for child in children:
            cand = child.metadata_payload["memory_candidate"]
            assert cand["requires_verification"] is False
            assert cand["last_verified_at"] is not None


def test_trace_extraction_remember_instruction_deduplication(session_factory) -> None:
    """Test that remember instructions in task finalized content are not extracted twice."""
    with session_scope(session_factory) as session:
        user = User(external_user_id="test-user")
        session.add(user)
        session.flush()
        conv = ConversationSession(
            user_id=user.id, channel="test", external_thread_id="thread-dedup"
        )
        session.add(conv)
        session.flush()
        task = Task(
            id="task-dedup",
            session_id=conv.id,
            task_text="Remember to use python 3.12 always.",
            repo_url="repo-dedup",
        )
        session.add(task)
        session.flush()
        task_id = task.id

        obs_repo = ObservationRepository(session)
        # Seed task_finalized observation that contains the same remember sentence
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="orchestrator",
            event_type="task_finalized",
            summary="Task finalized summary.",
            content="Task objective: Remember to use python 3.12 always.",
            admission_status="not_required",
        )
        session.flush()

    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    with session_scope(session_factory) as session:
        children = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate"
                )
            ).all()
        )
        # We expect only ONE candidate (from the task_text).
        # The task_finalized event should be skipped for remember instruction extraction.
        assert len(children) == 1
        cand = children[0].metadata_payload["memory_candidate"]
        assert cand["value"]["instruction"] == "Remember to use python 3.12 always."
