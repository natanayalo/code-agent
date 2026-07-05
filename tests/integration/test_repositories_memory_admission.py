"""Integration tests for memory admission decision persistence."""

from __future__ import annotations

from sqlalchemy.dialects import sqlite

from db.models import MemoryObservation, Session, Task, User
from repositories import MemoryAdmissionDecisionRepository, session_scope


def test_memory_admission_decision_repository_creates_and_filters(session_factory) -> None:
    """Admission decisions should be inspectable by task or session."""
    with session_scope(session_factory) as session:
        repo = MemoryAdmissionDecisionRepository(session)
        first = repo.create(
            category="project",
            memory_key="test_command",
            candidate_payload={"memory_key": "test_command"},
            decision="create",
            risk_level="low",
            reason="low-risk evidenced project memory can be created.",
            task_id="task-1",
            session_id="session-1",
            durable_memory_id="memory-1",
        )
        second = repo.create(
            category="personal",
            memory_key="communication_preference",
            candidate_payload={"memory_key": "communication_preference"},
            decision="needs_human_review",
            risk_level="medium",
            reason="personal memory requires human review.",
            task_id="task-2",
            session_id="session-1",
            proposal_id="proposal-1",
        )

        by_task = repo.list(task_id="task-1")
        by_session = repo.list(session_id="session-1")

    assert by_task == [first]
    assert {row.id for row in by_session} == {first.id, second.id}


def test_memory_admission_decision_repository_filters_by_repo_url_fallbacks(
    session_factory,
) -> None:
    """Repo filtering should check candidate payload first, then observation/task fallback."""
    with session_scope(session_factory) as session:
        user = User(external_user_id="user-1", display_name="Test User")
        session.add(user)
        session.flush()
        convo = Session(user_id=user.id, channel="http", external_thread_id="thread-1")
        session.add(convo)
        session.flush()
        task = Task(
            session_id=convo.id,
            task_text="test task",
            repo_url="https://github.com/org/task-repo",
            constraints={},
            budget={},
            secrets={},
            trace_context={},
        )
        session.add(task)
        session.flush()
        observation = MemoryObservation(
            task_id=task.id,
            session_id=convo.id,
            repo_url="https://github.com/org/observation-repo",
            source="worker",
            event_type="worker_completed",
            observed_at=task.created_at,
            summary="Worker completed",
            content="details",
            metadata_payload={},
            privacy_stripped=False,
            admission_status="processed",
        )
        session.add(observation)
        session.flush()

        repo = MemoryAdmissionDecisionRepository(session)
        explicit = repo.create(
            category="project",
            memory_key="explicit_repo",
            candidate_payload={"repo_url": "https://github.com/org/candidate-repo"},
            decision="create",
            risk_level="low",
            reason="explicit repo url",
            task_id=task.id,
            session_id=convo.id,
        )
        via_observation = repo.create(
            category="project",
            memory_key="observation_repo",
            candidate_payload={},
            decision="merge",
            risk_level="low",
            reason="observation repo fallback",
            task_id=task.id,
            session_id=convo.id,
            source_observation_id=observation.id,
        )
        via_task = repo.create(
            category="project",
            memory_key="task_repo",
            candidate_payload={},
            decision="update",
            risk_level="low",
            reason="task repo fallback",
            task_id=task.id,
            session_id=convo.id,
        )

        explicit_rows = repo.list(repo_url="https://github.com/org/candidate-repo")
        observation_rows = repo.list(repo_url="https://github.com/org/observation-repo")
        task_rows = repo.list(repo_url="https://github.com/org/task-repo")

    assert [row.id for row in explicit_rows] == [explicit.id]
    assert [row.id for row in observation_rows] == [via_observation.id]
    assert [row.id for row in task_rows] == [via_task.id]


def test_memory_admission_decision_repository_only_joins_repo_tables_for_repo_filters(
    session_factory,
    monkeypatch,
) -> None:
    """Repo joins should only be added when repo fallback filtering is requested."""
    with session_scope(session_factory) as session:
        repo = MemoryAdmissionDecisionRepository(session)
        repo.create(
            category="project",
            memory_key="test_command",
            candidate_payload={"memory_key": "test_command"},
            decision="create",
            risk_level="low",
            reason="low-risk evidenced project memory can be created.",
        )
        session.flush()

        statements: list[str] = []
        original_scalars = session.scalars

        def recording_scalars(statement, *args, **kwargs):
            statements.append(
                str(
                    statement.compile(
                        dialect=sqlite.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                )
            )
            return original_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", recording_scalars)

        repo.list()
        repo.list(repo_url="https://github.com/org/repo")

    assert "JOIN memory_observations" not in statements[0]
    assert "JOIN tasks" not in statements[0]
    assert "JOIN memory_observations" in statements[1]
    assert "JOIN tasks" in statements[1]
