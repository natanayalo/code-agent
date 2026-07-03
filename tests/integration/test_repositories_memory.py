"""Integration tests for personal and project memory repositories."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from db.models import PersonalMemory, ProjectMemory
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


@pytest.fixture
def postgres_session_factory():
    """Create a Postgres-backed session factory after applying Alembic migrations."""
    database_url = os.getenv("CODE_AGENT_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip(
            "CODE_AGENT_TEST_POSTGRES_URL is not set; " "skipping Postgres search integration."
        )

    base_url = make_url(database_url)
    admin_url = base_url.set(database="postgres")
    database_name = f"code_agent_test_{uuid4().hex}"
    admin_engine = create_engine_from_url(
        admin_url.render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )
    engine = None
    database_created = False

    try:
        try:
            with admin_engine.connect() as connection:
                connection.exec_driver_sql(f"CREATE DATABASE {database_name}")
            database_created = True
        except OperationalError as exc:
            pytest.skip(f"Postgres unavailable for search integration: {exc}")

        test_database_url = base_url.set(database=database_name).render_as_string(
            hide_password=False
        )
        config = Config(str(Path("alembic.ini").resolve()))
        config.set_main_option("script_location", str(Path("db/migrations").resolve()))
        config.set_main_option("sqlalchemy.url", test_database_url)
        command.upgrade(config, "head")

        engine = create_engine_from_url(test_database_url)
        yield create_session_factory(engine)
    finally:
        if engine is not None:
            engine.dispose()
        if database_created:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        """
                        SELECT pg_terminate_backend(pid)
                        FROM pg_stat_activity
                        WHERE datname = :database_name
                          AND pid <> pg_backend_pid()
                        """
                    ),
                    {"database_name": database_name},
                )
                connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {database_name}")
        admin_engine.dispose()


def test_memory_repositories_support_upsert_and_delete(session_factory) -> None:
    """Personal and project memory entries support CRUD operations."""
    with session_scope(session_factory) as session:
        personal_memory_repo = PersonalMemoryRepository(session)
        project_memory_repo = ProjectMemoryRepository(session)

        personal_memory_repo.upsert(
            memory_key="communication_preferences",
            value={"style": "concise"},
        )
        personal_memory_repo.upsert(
            memory_key="communication_preferences",
            value={"style": "direct"},
        )
        project_memory_repo.upsert(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="known_pitfalls",
            value={"docker": "use cert.pem when needed"},
        )

        personal_memory = personal_memory_repo.get(
            memory_key="communication_preferences",
        )
        project_memory = project_memory_repo.get(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="known_pitfalls",
        )

        assert personal_memory is not None
        assert personal_memory.value == {"style": "direct"}
        assert project_memory is not None
        assert project_memory.value == {"docker": "use cert.pem when needed"}
        assert len(personal_memory_repo.list_all()) == 1
        assert (
            len(project_memory_repo.list_by_repo("https://github.com/natanayalo/code-agent")) == 1
        )
        assert personal_memory_repo.delete(
            memory_key="communication_preferences",
        )
        assert project_memory_repo.delete(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="known_pitfalls",
        )


def test_memory_repositories_list_all_and_delete_missing_rows(session_factory) -> None:
    """Memory listing filters and delete-miss paths should be stable for operator UIs."""
    with session_scope(session_factory) as session:
        personal_memory_repo = PersonalMemoryRepository(session)
        project_memory_repo = ProjectMemoryRepository(session)

        personal_memory_repo.upsert(
            memory_key="editor",
            value={"theme": "light"},
        )
        personal_memory_repo.upsert(
            memory_key="shell",
            value={"name": "zsh"},
        )
        project_memory_repo.upsert(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="build",
            value={"cmd": "pytest"},
        )
        project_memory_repo.upsert(
            repo_url="https://github.com/natanayalo/other",
            memory_key="build",
            value={"cmd": "npm test"},
        )

        assert len(personal_memory_repo.list_all(limit=10, offset=0)) == 2
        assert len(personal_memory_repo.list_all(limit=1, offset=1)) == 1
        assert (
            len(
                project_memory_repo.list_all(
                    repo_url="https://github.com/natanayalo/code-agent",
                    limit=10,
                    offset=0,
                )
            )
            == 1
        )
        assert (
            project_memory_repo.delete(
                repo_url="https://github.com/natanayalo/missing",
                memory_key="none",
            )
            is False
        )
        assert personal_memory_repo.delete(memory_key="missing") is False


def test_personal_memory_upsert_recovers_from_duplicate_insert_race(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate insert race updates the existing personal memory entry."""
    with session_scope(session_factory) as session:
        personal_memory_repo = PersonalMemoryRepository(session)

        existing_entry = PersonalMemory(
            memory_key="communication_preferences",
            value={"style": "concise"},
        )
        session.add(existing_entry)
        session.flush()

        original_get = personal_memory_repo.get
        get_calls = 0

        def stale_get(*, memory_key: str) -> PersonalMemory | None:
            nonlocal get_calls
            get_calls += 1
            if get_calls == 1:
                return None
            return original_get(memory_key=memory_key)

        monkeypatch.setattr(personal_memory_repo, "get", stale_get)

        updated_entry = personal_memory_repo.upsert(
            memory_key="communication_preferences",
            value={"style": "direct"},
        )

        stored_entry = original_get(
            memory_key="communication_preferences",
        )
        assert updated_entry.id == existing_entry.id
        assert stored_entry is not None
        assert stored_entry.value == {"style": "direct"}


def test_project_memory_upsert_recovers_from_duplicate_insert_race(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate insert race updates the existing project memory entry."""
    with session_scope(session_factory) as session:
        project_memory_repo = ProjectMemoryRepository(session)
        repo_url = "https://github.com/natanayalo/code-agent"

        existing_entry = ProjectMemory(
            repo_url=repo_url,
            memory_key="known_pitfalls",
            value={"docker": "use cert.pem when needed"},
        )
        session.add(existing_entry)
        session.flush()

        original_get = project_memory_repo.get
        get_calls = 0

        def stale_get(*, repo_url: str, memory_key: str) -> ProjectMemory | None:
            nonlocal get_calls
            get_calls += 1
            if get_calls == 1:
                return None
            return original_get(repo_url=repo_url, memory_key=memory_key)

        monkeypatch.setattr(project_memory_repo, "get", stale_get)

        updated_entry = project_memory_repo.upsert(
            repo_url=repo_url,
            memory_key="known_pitfalls",
            value={"docker": "updated after retry"},
        )

        stored_entry = original_get(
            repo_url=repo_url,
            memory_key="known_pitfalls",
        )
        assert updated_entry.id == existing_entry.id
        assert stored_entry is not None
        assert stored_entry.value == {"docker": "updated after retry"}


def _seed_postgres_search_data(session) -> tuple[str, str, str]:
    repo_url = "https://github.com/natanayalo/code-agent"
    personal_memory_repo = PersonalMemoryRepository(session)
    project_memory_repo = ProjectMemoryRepository(session)

    personal_memory_repo.upsert(
        memory_key="pytest_playbook",
        value={"cmd": "pytest pytest tests/unit/test_memory_search.py"},
    )
    personal_memory_repo.upsert(
        memory_key="pytest_command",
        value={"cmd": ".venv/bin/pytest"},
    )
    personal_memory_repo.upsert(
        memory_key="shell",
        value={"name": "zsh"},
    )
    project_memory_repo.upsert(
        repo_url=repo_url,
        memory_key="pytest_matrix",
        value={"cmd": "pytest pytest tests/integration/test_repositories_memory.py"},
    )
    project_memory_repo.upsert(
        repo_url=repo_url,
        memory_key="pytest_command",
        value={"cmd": ".venv/bin/pytest"},
    )
    project_memory_repo.upsert(
        repo_url=repo_url,
        memory_key="lint_command",
        value={"cmd": ".venv/bin/ruff check ."},
    )
    session.flush()
    return repo_url, "pytest_playbook", "pytest_matrix"


def _search_vector_text(
    session,
    *,
    table_name: str,
    memory_key: str,
    filter_key: str | None = None,
    filter_value: str | None = None,
) -> str:
    filter_sql = f"{filter_key} = :filter_value AND " if filter_key else ""
    params = {"memory_key": memory_key}
    if filter_key:
        params["filter_value"] = filter_value or ""
    return session.execute(
        text(
            f"""
            SELECT search_vector::text
            FROM {table_name}
            WHERE {filter_sql}memory_key = :memory_key
            """
        ),
        params,
    ).scalar_one()


def test_memory_search_executes_real_postgres_fts(postgres_session_factory) -> None:
    """Full-text search should execute against Postgres with ranked results and headlines."""
    with session_scope(postgres_session_factory) as session:
        repo_url, personal_key, project_key = _seed_postgres_search_data(session)
        personal_search_vector = _search_vector_text(
            session,
            table_name="memory_personal",
            memory_key=personal_key,
        )
        project_search_vector = _search_vector_text(
            session,
            table_name="memory_project",
            filter_key="repo_url",
            filter_value=repo_url,
            memory_key=project_key,
        )

    with session_scope(postgres_session_factory) as session:
        personal_results = PersonalMemoryRepository(session).search(
            query="pytest",
            limit=5,
        )
        project_results = ProjectMemoryRepository(session).search(
            repo_url=repo_url,
            query="pytest",
            limit=5,
        )

    assert "pytest" in personal_search_vector
    assert "pytest" in project_search_vector
    assert [result.memory.memory_key for result in personal_results] == [
        "pytest_playbook",
        "pytest_command",
    ]
    assert [result.memory.memory_key for result in project_results] == [
        "pytest_matrix",
        "pytest_command",
    ]
    assert "__CA_MARK_START__" in (personal_results[0].headline or "")
    assert "__CA_MARK_END__" in (personal_results[0].headline or "")
    assert "__CA_MARK_START__" in (project_results[0].headline or "")
    assert "__CA_MARK_END__" in (project_results[0].headline or "")
