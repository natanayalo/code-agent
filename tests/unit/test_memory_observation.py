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
    _extract_candidates_from_task_text,
    _extract_remember_sentences,
    _get_base_executable_and_target,
    _is_verification_command,
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


def _seed_task(
    session,
    *,
    task_id: str = "task-1",
    task_text: str = "Implement code",
    repo_url: str = "repo1",
    external_thread_id: str = "thread-1",
    task_spec: dict | None = None,
    constraints: dict | None = None,
) -> Task:
    user = User(external_user_id="test-user")
    session.add(user)
    session.flush()
    conv = ConversationSession(
        user_id=user.id, channel="test", external_thread_id=external_thread_id
    )
    session.add(conv)
    session.flush()
    task = Task(
        id=task_id,
        session_id=conv.id,
        task_text=task_text,
        repo_url=repo_url,
        task_spec=task_spec,
        constraints=constraints,
    )
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
        assert retrieved.metadata_payload["verifier_outcome"] == {}


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
        assert len(children) == 1
        cand_val = children[0].metadata_payload["memory_candidate"]["value"]
        assert "pytest tests/unit" in cand_val
        assert "poetry run python test_script.py" in cand_val
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


def test_trace_extraction_pitfall_skips_none_exit_code(session_factory) -> None:
    """Verify that commands with None exit codes are skipped in pitfall extraction."""
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
                    {"command": "python test_script.py", "exit_code": None},
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
        # Should not extract a pitfall since fail_exit_code is None.
        # It only extracts poetry run python test_script.py (exit code 0) as a verification
        # command candidate.
        pitfalls = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "known_pitfalls"
        ]
        assert len(pitfalls) == 0


def test_trace_extraction_pitfall_skips_identical_successful_command(
    session_factory,
) -> None:
    """Verify that identical failed and successful commands do not create a pitfall."""
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
                    {"command": "pytest", "exit_code": 1},
                    {"command": "pytest", "exit_code": 0},
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
        pitfalls = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "known_pitfalls"
        ]
        assert len(pitfalls) == 0


def test_trace_extraction_skips_malformed_command_entries(session_factory) -> None:
    """Verify malformed command payload entries do not crash extraction."""
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
                    "not-a-dict",
                    {"command": 123, "exit_code": 0},
                    {"command": "pytest", "exit_code": 0},
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
        assert any(
            c.metadata_payload["memory_candidate"]["memory_key"] == "verification_commands"
            for c in children
        )


def test_trace_extraction_deduplicates_identical_candidates_before_save(session_factory) -> None:
    """Verify duplicate extracted candidates are only saved once."""
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
                    {"command": "pytest", "exit_code": 0},
                    {"command": "pytest", "exit_code": 0},
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
        verification_candidates = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "verification_commands"
        ]
        assert len(verification_candidates) == 1


def test_trace_extraction_deduplicates_duplicate_task_text_sentences(session_factory) -> None:
    """Verify repeated remember sentences in task text are only extracted once."""
    with session_scope(session_factory) as session:
        task = _seed_task(
            session,
            task_id="task-remember-dupe",
            task_text=("Remember to keep tests focused. Remember to keep tests focused."),
        )
        task_id = task.id

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
        remembered = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "remembered_instruction"
        ]
        assert len(remembered) == 1


def test_extract_candidates_from_task_text_does_not_query_existing_children(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that task-text extraction reuses the caller's parent-id set."""
    with session_scope(session_factory) as session:
        task = _seed_task(session, task_text="Remember to keep tests focused.")

        def fail_scalars(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("unexpected database lookup")

        monkeypatch.setattr(session, "scalars", fail_scalars)
        obs_repo = SimpleNamespace(create=lambda *args, **kwargs: None)

        _extract_candidates_from_task_text(
            session,
            task.id,
            task,
            obs_repo,
            extracted_parent_ids=set(),
        )


def test_trace_extraction_ignores_non_dict_existing_child_metadata(
    session_factory,
) -> None:
    """Verify existing child rows with malformed metadata are ignored safely."""
    with session_scope(session_factory) as session:
        task = _seed_task(session)
        task_id = task.id

        obs_repo = ObservationRepository(session)
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="system",
            event_type="extracted_candidate",
            summary="Malformed child",
            content="Bad child metadata",
            metadata_payload=["bad"],
            admission_status="pending",
        )
        obs_repo.create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker completed.",
            content="Trace:",
            metadata_payload={"commands_run": []},
            admission_status="not_required",
        )
        session.flush()

    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)


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
        task = _seed_task(
            session,
            task_id="task-2",
            task_text="Remember to use python 3.12 always.",
            repo_url="repo2",
            external_thread_id="thread-2",
        )
        task_id = task.id

        ObservationRepository(session).create(
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


def test_extract_remember_sentences_preserves_common_abbreviations() -> None:
    """Remember sentence splitting should not truncate common abbreviations."""
    sentences = _extract_remember_sentences(
        "Remember to use e.g. pytest when needed. Always use i.e. exact examples."
    )

    assert sentences == [
        "Remember to use e.g. pytest when needed.",
        "Always use i.e. exact examples.",
    ]


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
        task = _seed_task(
            session,
            task_id="task-custom-e2e",
            task_text="Run normal workflow.",
            repo_url="repo-custom",
            external_thread_id="thread-e2e",
            task_spec={"verification_commands": ["make build", "make ci"]},
            constraints={"verification_commands": ["make deploy-check"]},
        )
        task_id = task.id

        ObservationRepository(session).create(
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
        # We expect one aggregated child containing build and deploy-check keys
        assert len(children) == 1
        cand_val = children[0].metadata_payload["memory_candidate"]["value"]
        assert "make build" in cand_val
        assert "make deploy-check" in cand_val

        cand = children[0].metadata_payload["memory_candidate"]
        assert cand["requires_verification"] is False
        assert cand["last_verified_at"] is not None


def test_trace_extraction_recognizes_unittest_module_commands(session_factory) -> None:
    """Verify that unittest module invocations are extracted as verification commands."""
    with session_scope(session_factory) as session:
        task = _seed_task(
            session,
            task_id="task-unittest",
            task_text="Run the test suite.",
            repo_url="repo-unittest",
            external_thread_id="thread-unittest",
            task_spec={"verification_commands": ["python -m unittest discover"]},
        )
        task_id = task.id

        ObservationRepository(session).create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker finished.",
            content="Trace",
            metadata_payload={
                "commands_run": [
                    {"command": "python -m unittest discover", "exit_code": 0},
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
                    MemoryObservation.event_type == "extracted_candidate",
                    MemoryObservation.task_id == task_id,
                )
            ).all()
        )
        assert len(children) == 1
        candidate = children[0].metadata_payload["memory_candidate"]
        assert candidate["memory_key"] == "verification_commands"
        assert candidate["value"] == {"python -m unittest discover": "python -m unittest discover"}


def test_is_verification_command_strips_nested_prefixes() -> None:
    """Verify nested wrapper prefixes still resolve to the underlying test command."""
    assert _is_verification_command("poetry run python -m unittest discover") is True
    assert _is_verification_command("npminstall") is False


def test_trace_extraction_pitfall_requires_same_full_command(session_factory) -> None:
    """Verify different scripts under the same runner do not create a pitfall."""
    with session_scope(session_factory) as session:
        task = _seed_task(
            session,
            task_id="task-different-scripts",
            task_text="Run tests.",
            repo_url="repo-different-scripts",
            external_thread_id="thread-different-scripts",
        )
        task_id = task.id

        ObservationRepository(session).create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker finished.",
            content="Trace",
            metadata_payload={
                "commands_run": [
                    {"command": "python script_a.py", "exit_code": 1},
                    {"command": "python script_b.py", "exit_code": 0},
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
                    MemoryObservation.event_type == "extracted_candidate",
                    MemoryObservation.task_id == task_id,
                )
            ).all()
        )
        pitfalls = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "known_pitfalls"
        ]
        assert len(pitfalls) == 0


def test_trace_extraction_uses_deterministic_verifier_outcome(session_factory) -> None:
    """Native runs should extract verifier commands from persisted verifier outcome metadata."""
    with session_scope(session_factory) as session:
        task = _seed_task(
            session,
            task_id="task-native-verifier",
            task_text="Run native workflow.",
            repo_url="repo-native",
            external_thread_id="thread-e2e",
            task_spec={"verification_commands": ["python3 --version"]},
        )
        task_id = task.id

        ObservationRepository(session).create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Native worker finished.",
            content="Native wrapper command only.",
            metadata_payload={
                "commands_run": [
                    {"command": "codex exec --model gpt-5 -", "exit_code": 0},
                ],
                "verifier_outcome": {
                    "status": "warning",
                    "deterministic_verification": {
                        "status": "passed",
                        "commands": ["python3 --version"],
                        "passed_commands": ["python3 --version"],
                    },
                },
            },
            admission_status="not_required",
        )
        session.flush()

    with session_scope(session_factory) as session:
        summary = ObservationMemoryBridge.bridge_observations(session, task_id)

    with session_scope(session_factory) as session:
        children = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate",
                    MemoryObservation.task_id == task_id,
                )
            ).all()
        )
        assert len(children) == 1
        candidate = children[0].metadata_payload["memory_candidate"]
        assert candidate["memory_key"] == "verification_commands"
        assert candidate["value"] == {"python3 --version": "python3 --version"}
        assert candidate["requires_verification"] is False
        assert candidate["last_verified_at"] is not None
        assert summary["decision_counts"] == {"create": 1}
        assert summary["durable_memory_count"] == 1


def test_trace_extraction_remember_instruction_deduplication(session_factory) -> None:
    """Test that remember instructions in task finalized content are not extracted twice."""
    with session_scope(session_factory) as session:
        task = _seed_task(
            session,
            task_id="task-dedup",
            task_text="Remember to use python 3.12 always.",
            repo_url="repo-dedup",
            external_thread_id="thread-dedup",
        )
        task_id = task.id

        ObservationRepository(session).create(
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


def test_verification_command_exclusions() -> None:
    """Test exclusions for help/version flags and build/setup/lint commands."""
    # Exclude help/version
    assert _is_verification_command("pytest --help") is False
    assert _is_verification_command("pytest -h") is False
    assert _is_verification_command("vitest --version") is False
    assert _is_verification_command("python test.py --version") is False

    # Exclude build/lint/format/setup
    assert _is_verification_command("pip install pytest") is False
    assert _is_verification_command("poetry run ruff check .") is False
    assert _is_verification_command("npm install") is False
    assert _is_verification_command("black app.py") is False
    assert _is_verification_command("python setup.py install") is False
    assert _is_verification_command("python3 setup.py build") is False

    # Allowed commands
    assert _is_verification_command("pytest tests/unit") is True
    assert _is_verification_command("go test ./...") is True
    assert _is_verification_command("cargo test") is True
    assert _is_verification_command("python3 -m unittest discover") is True


def test_verification_command_exact_override(session_factory) -> None:
    """Verify that lint/setup commands are extracted if they exactly match verification commands."""
    with session_scope(session_factory) as session:
        task = _seed_task(
            session,
            task_id="task-override-exact",
            task_text="Run verification.",
            repo_url="repo-exact",
            task_spec={
                "verification_commands": ["ruff check .", "pip install -r requirements.txt"]
            },
        )
        task_id = task.id

        ObservationRepository(session).create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Completed.",
            content="Trace",
            metadata_payload={
                "commands_run": [
                    {"command": "ruff check .", "exit_code": 0},
                    {
                        "command": "ruff check ./subdir",
                        "exit_code": 0,
                    },  # Not exact match, should be excluded
                    {"command": "pip install -r requirements.txt", "exit_code": 0},
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
                    MemoryObservation.event_type == "extracted_candidate",
                    MemoryObservation.task_id == task_id,
                )
            ).all()
        )
        assert len(children) == 1
        cand_val = children[0].metadata_payload["memory_candidate"]["value"]
        assert "ruff check ." in cand_val
        assert "pip install -r requirements.txt" in cand_val
        assert "ruff check ./subdir" not in cand_val


def test_pitfall_conservative_matching(session_factory) -> None:
    """Test conservative pitfall extraction rules."""
    with session_scope(session_factory) as session:
        task = _seed_task(session, task_id="task-pitfalls-harness")
        task_id = task.id

        ObservationRepository(session).create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Worker completed.",
            content="Trace:",
            metadata_payload={
                "commands_run": [
                    # 1. Matches: same target, different wrapper (pytest vs poetry run pytest)
                    {"command": "python script_a.py", "exit_code": 1},
                    {"command": "poetry run python script_a.py", "exit_code": 0},
                    # 2. Excluded: unrelated classes (non-ver command vs ver command)
                    {"command": "python app.py", "exit_code": 1},
                    {"command": "pytest", "exit_code": 0},
                    # 3. Excluded: different targets, same base executable (python)
                    {"command": "python script_b.py", "exit_code": 1},
                    {"command": "python script_c.py", "exit_code": 0},
                    # 4. Excluded: identical command fail/success
                    {"command": "pytest", "exit_code": 1},
                    {"command": "pytest", "exit_code": 0},
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
                    MemoryObservation.event_type == "extracted_candidate",
                    MemoryObservation.task_id == task_id,
                )
            ).all()
        )
        # We expect:
        # - 1 verification command: poetry run python script_a.py
        # - 1 verification command: pytest
        # - 1 pitfall: python script_a.py -> poetry run python script_a.py
        pitfalls = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "known_pitfalls"
        ]
        assert len(pitfalls) == 1
        val = pitfalls[0].metadata_payload["memory_candidate"]["value"]
        assert val["failed_command"] == "python script_a.py"
        assert val["corrected_command"] == "poetry run python script_a.py"


def test_remember_instruction_expanded_and_guards(session_factory) -> None:
    """Test remember instruction triggers, length constraints, and source guards."""
    # 1. Triggers and length constraints
    assert _extract_remember_sentences(
        "Remember to keep functions clean.", is_operator_input=True
    ) == ["Remember to keep functions clean."]
    assert _extract_remember_sentences("Make sure to check coverage.", is_operator_input=True) == [
        "Make sure to check coverage."
    ]
    assert _extract_remember_sentences("Ensure you document APIs.", is_operator_input=True) == [
        "Ensure you document APIs."
    ]
    assert _extract_remember_sentences("Should always run tests.", is_operator_input=True) == [
        "Should always run tests."
    ]
    assert _extract_remember_sentences("Should never use mock.", is_operator_input=True) == [
        "Should never use mock."
    ]

    # "do not" trigger source checks
    assert _extract_remember_sentences(
        "Do not modify database directly.", is_operator_input=True
    ) == ["Do not modify database directly."]
    assert (
        _extract_remember_sentences("Do not modify database directly.", is_operator_input=False)
        == []
    )

    # Length constraints: short/long
    assert _extract_remember_sentences("Remember.", is_operator_input=True) == []  # < 10 chars
    long_txt = "Remember to " + ("a" * 200)
    assert _extract_remember_sentences(long_txt, is_operator_input=True) == []  # > 200 chars


def test_convention_guideline_and_policy(session_factory) -> None:
    """Test convention extraction with guideline and policy prefixes."""
    with session_scope(session_factory) as session:
        task = _seed_task(session, task_id="task-conventions")
        task_id = task.id

        ObservationRepository(session).create(
            task_id=task_id,
            session_id=task.session_id,
            repo_url=task.repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Finished task.",
            content="Some details.\nguideline: use spaces.\npolicy: run checks.",
            admission_status="not_required",
        )
        session.flush()

    with session_scope(session_factory) as session:
        ObservationMemoryBridge.bridge_observations(session, task_id)

    with session_scope(session_factory) as session:
        children = list(
            session.scalars(
                select(MemoryObservation).where(
                    MemoryObservation.event_type == "extracted_candidate",
                    MemoryObservation.task_id == task_id,
                )
            ).all()
        )
        conventions = [
            c
            for c in children
            if c.metadata_payload["memory_candidate"]["memory_key"] == "repo_convention"
        ]
        assert len(conventions) == 2
        vals = {c.metadata_payload["memory_candidate"]["value"]["convention"] for c in conventions}
        assert "use spaces." in vals
        assert "run checks." in vals


def test_get_base_executable_and_target_generic_interpreters() -> None:
    """Test that _get_base_executable_and_target recursively strips nested generic interpreters."""
    assert _get_base_executable_and_target("pytest tests/unit") == ("pytest", "tests/unit")
    assert _get_base_executable_and_target("python script.py") == ("script.py", None)
    assert _get_base_executable_and_target("python3 -m unittest discover") == (
        "unittest",
        "discover",
    )
    assert _get_base_executable_and_target("sudo python script.py") == ("script.py", None)
    assert _get_base_executable_and_target("sudo python3 -m pytest tests/unit") == (
        "pytest",
        "tests/unit",
    )
