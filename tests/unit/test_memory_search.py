"""Unit tests for skeptical-memory search helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.pool import StaticPool

from db.base import Base
from db.models import PersonalMemory, ProjectMemory
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


class _FakeMappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self) -> _FakeMappingsResult:
        return self

    def all(self):
        return self._rows


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, *, dialect_name: str, execute_rows, scalar_rows):
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))
        self.bind = self._bind
        self._execute_rows = execute_rows
        self._scalar_rows = scalar_rows
        self.last_execute_params = None
        self.last_execute_sql = None

    def get_bind(self):
        return self._bind

    def execute(self, statement, params):
        self.last_execute_sql = str(statement)
        self.last_execute_params = params
        return _FakeMappingsResult(self._execute_rows)

    def scalars(self, _statement):
        return _FakeScalarResult(self._scalar_rows)


def _sqlite_session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_search_empty_query_returns_empty() -> None:
    repo = PersonalMemoryRepository(
        _FakeSession(dialect_name="postgresql", execute_rows=[], scalar_rows=[])
    )

    assert repo.search(query="   ") == []


def test_search_sqlite_fallback() -> None:
    session_factory = _sqlite_session_factory()
    with session_scope(session_factory) as session:
        PersonalMemoryRepository(session).upsert(
            memory_key="communication_style",
            value={"style": "concise"},
        )

    with session_scope(session_factory) as session:
        results = PersonalMemoryRepository(session).search(
            query="concise",
            limit=20,
        )

    assert [result.memory.memory_key for result in results] == ["communication_style"]
    assert results[0].headline is None


def test_search_sqlite_fallback_filters_and_respects_limit() -> None:
    session_factory = _sqlite_session_factory()
    with session_scope(session_factory) as session:
        repo_url = "https://github.com/natanayalo/code-agent"
        PersonalMemoryRepository(session).upsert(
            memory_key="pytest_playbook",
            value={"cmd": ".venv/bin/pytest tests/unit/test_memory_search.py"},
        )
        PersonalMemoryRepository(session).upsert(
            memory_key="shell",
            value={"name": "zsh"},
        )
        PersonalMemoryRepository(session).upsert(
            memory_key="test_command",
            value={"cmd": ".venv/bin/pytest"},
        )
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="pytest_matrix",
            value={"cmd": "pytest tests/integration/test_repositories_memory.py"},
        )
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="lint_command",
            value={"cmd": ".venv/bin/ruff check ."},
        )

    with session_scope(session_factory) as session:
        personal_results = PersonalMemoryRepository(session).search(
            query="playbook",
            limit=1,
        )
        project_results = ProjectMemoryRepository(session).search(
            repo_url=repo_url,
            query="pytest",
            limit=5,
        )

    assert [result.memory.memory_key for result in personal_results] == ["pytest_playbook"]
    assert [result.memory.memory_key for result in project_results] == ["pytest_matrix"]


def test_search_respects_limit_cap() -> None:
    session = _FakeSession(dialect_name="postgresql", execute_rows=[], scalar_rows=[])

    PersonalMemoryRepository(session).search(query="memory", limit=999)

    assert session.last_execute_params["limit"] == 100


def test_search_truncates_query_before_normalizing() -> None:
    session = _FakeSession(dialect_name="postgresql", execute_rows=[], scalar_rows=[])

    PersonalMemoryRepository(session).search(
        query=f"{'x' * 200}{' y' * 50}",
        limit=20,
    )

    assert session.last_execute_params["query"] == "x" * 200


def test_search_queries_operator_global_personal_memory() -> None:
    memory = PersonalMemory(
        id="pm-1",
        memory_key="test_command",
        value={"cmd": ".venv/bin/pytest"},
        source="operator",
        confidence=0.8,
        scope="global",
        last_verified_at=datetime(2026, 7, 3, tzinfo=UTC),
        requires_verification=False,
    )
    session = _FakeSession(
        dialect_name="postgresql",
        execute_rows=[{"id": "pm-1", "headline": "Use __CA_MARK_START__pytest__CA_MARK_END__"}],
        scalar_rows=[memory],
    )

    results = PersonalMemoryRepository(session).search(
        query="pytest",
        limit=10,
    )

    assert "user_id" not in session.last_execute_params
    assert session.last_execute_params["query"] == "pytest"
    assert "memory_personal" in session.last_execute_sql
    assert "user_id" not in session.last_execute_sql
    assert results[0].memory.memory_key == "test_command"
    assert results[0].headline == "Use __CA_MARK_START__pytest__CA_MARK_END__"


def test_search_uses_session_get_bind_when_bind_is_missing() -> None:
    session = _FakeSession(
        dialect_name="postgresql",
        execute_rows=[{"id": "pm-1", "headline": None}],
        scalar_rows=[
            PersonalMemory(
                id="pm-1",
                memory_key="build_command",
                value={"cmd": ".venv/bin/pytest"},
            )
        ],
    )
    session.bind = None

    results = PersonalMemoryRepository(session).search(
        query="pytest",
        limit=10,
    )

    assert session.last_execute_params["query"] == "pytest"
    assert results[0].memory.memory_key == "build_command"


def test_search_matches_uuid_ids_from_postgres_rows() -> None:
    memory_id = uuid4()
    memory = PersonalMemory(
        id=memory_id,
        memory_key="test_command",
        value={"cmd": ".venv/bin/pytest"},
        source="operator",
        confidence=0.8,
        scope="global",
        last_verified_at=datetime(2026, 7, 3, tzinfo=UTC),
        requires_verification=False,
    )
    session = _FakeSession(
        dialect_name="postgresql",
        execute_rows=[{"id": memory_id, "headline": "Use __CA_MARK_START__pytest__CA_MARK_END__"}],
        scalar_rows=[memory],
    )

    results = PersonalMemoryRepository(session).search(
        query="pytest",
        limit=10,
    )

    assert [result.memory.id for result in results] == [memory_id]


def test_search_filters_by_repo_url() -> None:
    memory = ProjectMemory(
        id="pj-1",
        repo_url="https://github.com/natanayalo/code-agent",
        memory_key="known_pitfalls",
        value={"cmd": "npm run test:run"},
        source="operator",
        confidence=0.7,
        scope="repo",
        last_verified_at=datetime(2026, 7, 3, tzinfo=UTC),
        requires_verification=True,
    )
    session = _FakeSession(
        dialect_name="postgresql",
        execute_rows=[{"id": "pj-1", "headline": "Remember __CA_MARK_START__test__CA_MARK_END__"}],
        scalar_rows=[memory],
    )

    results = ProjectMemoryRepository(session).search(
        repo_url="https://github.com/natanayalo/code-agent",
        query="test",
        limit=10,
    )

    assert session.last_execute_params["repo_url"] == "https://github.com/natanayalo/code-agent"
    assert session.last_execute_params["query"] == "test"
    assert "memory_project" in session.last_execute_sql
    assert results[0].memory.memory_key == "known_pitfalls"
    assert results[0].headline == "Remember __CA_MARK_START__test__CA_MARK_END__"


def test_models_sqlite_compatible() -> None:
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(engine)
