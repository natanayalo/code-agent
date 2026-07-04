"""Unit tests for the SQLite fallback behavior of ObservationRepository."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from repositories import (
    ObservationRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


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


def test_create_and_get(session_factory) -> None:
    """We can create and retrieve observations with all fields."""
    now = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)
    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        obs = repo.create(
            source="worker",
            event_type="worker_completed",
            summary="Worker run finished",
            content="Detailed logs",
            task_id="task-123",
            session_id="session-456",
            repo_url="https://github.com/org/repo",
            worker_type="gemini",
            observed_at=now,
            metadata_payload={"commands": ["ls"]},
            privacy_stripped=True,
            admission_status="pending",
        )
        session.flush()
        obs_id = obs.id

    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        retrieved = repo.get(obs_id)
        assert retrieved is not None
        assert retrieved.id == obs_id
        assert retrieved.source == "worker"
        assert retrieved.event_type == "worker_completed"
        assert retrieved.summary == "Worker run finished"
        assert retrieved.content == "Detailed logs"
        assert retrieved.task_id == "task-123"
        assert retrieved.session_id == "session-456"
        assert retrieved.repo_url == "https://github.com/org/repo"
        assert retrieved.worker_type == "gemini"
        assert retrieved.observed_at.replace(tzinfo=UTC) == now
        assert retrieved.metadata_payload == {"commands": ["ls"]}
        assert retrieved.privacy_stripped is True
        assert retrieved.admission_status == "pending"


def test_list_timeline_scope_validation(session_factory) -> None:
    """list_timeline requires at least one scope parameter, and sorts by observed_at asc."""
    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)

        # 1. Validation error when both None
        with pytest.raises(
            ValueError,
            match="list_timeline requires at least one of session_id or task_id",
        ):
            repo.list_timeline()

        # Seed data
        repo.create(
            source="worker",
            event_type="test",
            summary="first",
            content="c1",
            task_id="t1",
            observed_at=datetime(2026, 7, 4, 10, 0, tzinfo=UTC),
        )
        repo.create(
            source="worker",
            event_type="test",
            summary="second",
            content="c2",
            task_id="t1",
            observed_at=datetime(2026, 7, 4, 11, 0, tzinfo=UTC),
        )
        repo.create(
            source="worker",
            event_type="test",
            summary="different task",
            content="c3",
            task_id="t2",
            observed_at=datetime(2026, 7, 4, 9, 0, tzinfo=UTC),
        )
        session.flush()

    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        t1_list = repo.list_timeline(task_id="t1")
        assert len(t1_list) == 2
        assert t1_list[0].summary == "first"
        assert t1_list[1].summary == "second"


def test_recent_scope_validation(session_factory) -> None:
    """recent requires at least one scope, and sorts by observed_at desc."""
    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)

        with pytest.raises(ValueError, match="recent requires at least one scope"):
            repo.recent()

        repo.create(
            source="worker",
            event_type="test",
            summary="oldest",
            content="c1",
            session_id="s1",
            observed_at=datetime(2026, 7, 4, 10, 0, tzinfo=UTC),
        )
        repo.create(
            source="worker",
            event_type="test",
            summary="newest",
            content="c2",
            session_id="s1",
            observed_at=datetime(2026, 7, 4, 11, 0, tzinfo=UTC),
        )
        session.flush()

    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        s1_recent = repo.recent(session_id="s1")
        assert len(s1_recent) == 2
        assert s1_recent[0].summary == "newest"
        assert s1_recent[1].summary == "oldest"


def test_search_scope_validation_and_sqlite_fallback(session_factory) -> None:
    """search requires at least one scope, and does substring matching on SQLite."""
    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)

        with pytest.raises(ValueError, match="search requires at least one scope"):
            repo.search(query="test")

        repo.create(
            source="worker",
            event_type="test",
            summary="Target word here",
            content="c1",
            repo_url="repo1",
        )
        repo.create(
            source="worker",
            event_type="test",
            summary="s2",
            content="target word here",
            repo_url="repo1",
        )
        repo.create(
            source="worker",
            event_type="test",
            summary="s3",
            content="no match",
            repo_url="repo1",
        )
        session.flush()

    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        matches = repo.search(query="target word", repo_url="repo1")
        assert len(matches) == 2
        assert {m.summary for m in matches} == {"Target word here", "s2"}


def test_update_admission_outcome(session_factory) -> None:
    """update_admission_outcome updates status, processed_at, and error."""
    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        obs = repo.create(
            source="worker",
            event_type="test",
            summary="test",
            content="test",
            task_id="t1",
            admission_status="pending",
        )
        session.flush()
        obs_id = obs.id

    now = datetime(2026, 7, 4, 15, 0, 0, tzinfo=UTC)
    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        repo.update_admission_outcome(
            observation_id=obs_id,
            status="failed",
            processed_at=now,
            error="Failed to admit",
        )

    with session_scope(session_factory) as session:
        repo = ObservationRepository(session)
        retrieved = repo.get(obs_id)
        assert retrieved is not None
        assert retrieved.admission_status == "failed"
        assert retrieved.admission_processed_at.replace(tzinfo=UTC) == now
        assert retrieved.admission_error == "Failed to admit"
