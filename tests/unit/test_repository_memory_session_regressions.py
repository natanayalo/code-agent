"""Focused regression tests for session and memory repositories."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from db.base import Base
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    UserRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from repositories.sqlalchemy import SessionStateRepository
from repositories.sqlalchemy_memory import _relaxed_tsquery


@pytest.fixture
def session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_session_repository_updates_active_task_touch_and_paginates(session_factory) -> None:
    """Session repository should update rows and clamp pagination bounds safely."""
    seen_at = datetime.now(UTC)

    with session_scope(session_factory) as session:
        user = UserRepository(session).create(
            external_user_id="session-user",
            display_name="Session User",
        )
        repo = SessionRepository(session)

        first = repo.create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-1",
        )
        second = repo.create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-2",
        )
        third = repo.create(
            user_id=user.id,
            channel="telegram",
            external_thread_id="thread-3",
        )

        updated = repo.set_active_task(session_id=second.id, active_task_id="task-99")
        touched = repo.touch(session_id=second.id, seen_at=seen_at)

        assert updated is not None
        assert updated.active_task_id == "task-99"
        assert touched is not None
        assert touched.last_seen_at == seen_at
        assert [row.id for row in repo.list_by_user(user.id)] == [first.id, second.id, third.id]
        assert [row.id for row in repo.list_all(limit=0, offset=-5)] == [third.id]
        assert [row.id for row in repo.list_all(limit=2, offset=1)] == [second.id, first.id]


def test_personal_memory_repository_preserves_unspecified_metadata_and_supports_list_delete(
    session_factory,
) -> None:
    """Personal memory updates should keep existing skeptical metadata unless explicitly changed."""
    verified_at = datetime(2026, 4, 1, tzinfo=UTC)

    with session_scope(session_factory) as session:
        repo = PersonalMemoryRepository(session)

        created = repo.upsert(
            memory_key="communication_preferences",
            value={"style": "concise"},
            source="operator-note",
            confidence=0.9,
            scope="repo",
            last_verified_at=verified_at,
            requires_verification=True,
        )
        updated = repo.upsert(
            memory_key="communication_preferences",
            value={"style": "direct"},
        )

        assert created.id == updated.id
        assert updated.value == {"style": "direct"}
        assert updated.source == "operator-note"
        assert updated.confidence == 0.9
        assert updated.scope == "repo"
        assert updated.last_verified_at == verified_at
        assert updated.requires_verification is True
        assert [row.id for row in repo.list_all(limit=5, offset=0)] == [updated.id]
        assert repo.delete(memory_key="communication_preferences") is True
        assert repo.delete(memory_key="communication_preferences") is False


def test_project_memory_repository_allows_explicit_metadata_clears_and_pagination(
    session_factory,
) -> None:
    """Project memory updates should allow explicit null/false metadata values."""
    with session_scope(session_factory) as session:
        repo = ProjectMemoryRepository(session)

        repo.upsert(
            repo_url="https://github.com/example/repo-a",
            memory_key="pitfall",
            value={"note": "use cert.pem"},
            source="retrospective",
            confidence=0.7,
            scope="branch",
            requires_verification=True,
        )
        updated = repo.upsert(
            repo_url="https://github.com/example/repo-a",
            memory_key="pitfall",
            value={"note": "use updated cert.pem"},
            source=None,
            confidence=0.4,
            scope=None,
            last_verified_at=None,
            requires_verification=False,
        )
        repo.upsert(
            repo_url="https://github.com/example/repo-b",
            memory_key="pitfall",
            value={"note": "different repo"},
        )

        assert updated.source is None
        assert updated.confidence == 0.4
        assert updated.scope is None
        assert updated.last_verified_at is None
        assert updated.requires_verification is False
        assert [row.repo_url for row in repo.list_by_repo("https://github.com/example/repo-a")] == [
            "https://github.com/example/repo-a"
        ]
        assert [row.repo_url for row in repo.list_all(limit=1, offset=1)] == [
            "https://github.com/example/repo-a"
        ]
        assert (
            repo.delete(
                repo_url="https://github.com/example/repo-a",
                memory_key="pitfall",
            )
            is True
        )
        assert (
            repo.delete(
                repo_url="https://github.com/example/repo-a",
                memory_key="pitfall",
            )
            is False
        )


def test_session_state_repository_recovers_from_integrity_error_on_upsert(monkeypatch) -> None:
    """Session-state upserts should recover when another writer inserts the row first."""

    @contextmanager
    def _nested():
        yield

    state = SimpleNamespace(
        session_id="session-1",
        active_goal="steady state",
        decisions_made={},
        identified_risks={},
        files_touched=[],
    )
    flush_calls = {"count": 0}

    class _FakeSession:
        def __init__(self) -> None:
            self.begin_nested = _nested

        def add(self, value) -> None:
            self.added = value

        def flush(self) -> None:
            flush_calls["count"] += 1
            if flush_calls["count"] == 1:
                raise IntegrityError("insert", {}, Exception("duplicate"))

    repo = SessionStateRepository(_FakeSession())  # type: ignore[arg-type]
    get_results = iter([None, state])
    monkeypatch.setattr(repo, "get", lambda session_id: next(get_results))

    recovered = repo.upsert(
        session_id="session-1",
        decisions_made={"worker": "codex"},
        files_touched=["workers/codex_cli_worker.py"],
    )

    assert recovered is state
    assert recovered.decisions_made == {"worker": "codex"}
    assert recovered.files_touched == ["workers/codex_cli_worker.py"]


def test_memory_repositories_recover_from_integrity_error_race(monkeypatch) -> None:
    """Memory upserts should recover if another writer creates the row first."""

    @contextmanager
    def _nested():
        yield

    class _FakeSession:
        def __init__(self) -> None:
            self.begin_nested = _nested
            self.flush_calls = 0

        def add(self, value) -> None:
            self.added = value

        def flush(self) -> None:
            self.flush_calls += 1
            if self.flush_calls == 1:
                raise IntegrityError("insert", {}, Exception("duplicate"))

    personal_state = SimpleNamespace(
        id="mem-1",
        value={"style": "old"},
        source="seed",
        confidence=0.1,
        scope="global",
        last_verified_at=None,
        requires_verification=True,
    )
    personal_repo = PersonalMemoryRepository(_FakeSession())  # type: ignore[arg-type]
    personal_get_results = iter([None, personal_state])
    monkeypatch.setattr(
        personal_repo,
        "get",
        lambda *, memory_key: next(personal_get_results),
    )

    recovered_personal = personal_repo.upsert(
        memory_key="communication",
        value={"style": "new"},
        source="operator",
    )
    assert recovered_personal is personal_state
    assert recovered_personal.value == {"style": "new"}
    assert recovered_personal.source == "operator"

    project_state = SimpleNamespace(
        id="proj-1",
        value={"pitfall": "old"},
        source="seed",
        confidence=0.2,
        scope="repo",
        last_verified_at=None,
        requires_verification=True,
    )
    project_repo = ProjectMemoryRepository(_FakeSession())  # type: ignore[arg-type]
    project_get_results = iter([None, project_state])
    monkeypatch.setattr(
        project_repo,
        "get",
        lambda *, repo_url, memory_key: next(project_get_results),
    )

    recovered_project = project_repo.upsert(
        repo_url="https://github.com/example/repo",
        memory_key="pitfall",
        value={"pitfall": "new"},
        requires_verification=False,
    )
    assert recovered_project is project_state
    assert recovered_project.value == {"pitfall": "new"}
    assert recovered_project.requires_verification is False


def test_relaxed_memory_search_query_uses_significant_or_terms() -> None:
    """Relaxed Postgres fallback should avoid requiring every task-goal term."""
    query = (
        "Probe memory_e2e_full_123. Create qa-memory-e2e.txt exactly. "
        "Run python3 --version for future tasks."
    )

    relaxed = _relaxed_tsquery(query)

    assert relaxed is not None
    assert "|" in relaxed
    assert "probe" in relaxed
    assert "python3" in relaxed
    assert "exactly" not in relaxed


def test_relaxed_memory_search_query_preserves_underscores_and_unicode() -> None:
    """Relaxed fallback should keep code identifiers and Unicode tokens intact."""
    relaxed = _relaxed_tsquery("Inspect test_memory_observation and café results")

    assert relaxed is not None
    assert "test_memory_observation" in relaxed
    assert "café" in relaxed
