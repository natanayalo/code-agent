"""Integration tests for personal and project memory repositories."""

from __future__ import annotations

import pytest

from db.models import PersonalMemory, ProjectMemory
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    UserRepository,
    session_scope,
)


def test_memory_repositories_support_upsert_and_delete(session_factory) -> None:
    """Personal and project memory entries support CRUD operations."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        personal_memory_repo = PersonalMemoryRepository(session)
        project_memory_repo = ProjectMemoryRepository(session)

        user = user_repo.create(external_user_id="telegram:mem", display_name="Memory User")
        personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="communication_preferences",
            value={"style": "concise"},
        )
        personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="communication_preferences",
            value={"style": "direct"},
        )
        project_memory_repo.upsert(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="known_pitfalls",
            value={"docker": "use cert.pem when needed"},
        )

        personal_memory = personal_memory_repo.get(
            user_id=user.id,
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
        assert len(personal_memory_repo.list_by_user(user.id)) == 1
        assert (
            len(project_memory_repo.list_by_repo("https://github.com/natanayalo/code-agent")) == 1
        )
        assert personal_memory_repo.delete(
            user_id=user.id,
            memory_key="communication_preferences",
        )
        assert project_memory_repo.delete(
            repo_url="https://github.com/natanayalo/code-agent",
            memory_key="known_pitfalls",
        )


def test_memory_repositories_list_all_and_delete_missing_rows(session_factory) -> None:
    """Memory listing filters and delete-miss paths should be stable for operator UIs."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        personal_memory_repo = PersonalMemoryRepository(session)
        project_memory_repo = ProjectMemoryRepository(session)

        user = user_repo.create(external_user_id="telegram:mem-list", display_name="Memory List")
        personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="editor",
            value={"theme": "light"},
        )
        personal_memory_repo.upsert(
            user_id=user.id,
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

        assert len(personal_memory_repo.list_all(user_id=user.id, limit=10, offset=0)) == 2
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
        assert personal_memory_repo.delete(user_id=user.id, memory_key="missing") is False


def test_personal_memory_upsert_recovers_from_duplicate_insert_race(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate insert race updates the existing personal memory entry."""
    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        personal_memory_repo = PersonalMemoryRepository(session)

        user = user_repo.create(external_user_id="telegram:race", display_name="Race User")
        existing_entry = PersonalMemory(
            user_id=user.id,
            memory_key="communication_preferences",
            value={"style": "concise"},
        )
        session.add(existing_entry)
        session.flush()

        original_get = personal_memory_repo.get
        get_calls = 0

        def stale_get(*, user_id: str, memory_key: str) -> PersonalMemory | None:
            nonlocal get_calls
            get_calls += 1
            if get_calls == 1:
                return None
            return original_get(user_id=user_id, memory_key=memory_key)

        monkeypatch.setattr(personal_memory_repo, "get", stale_get)

        updated_entry = personal_memory_repo.upsert(
            user_id=user.id,
            memory_key="communication_preferences",
            value={"style": "direct"},
        )

        stored_entry = original_get(
            user_id=user.id,
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
