"""Unit tests for skeptical memory and session state (Milestone 8)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.base import Base
from orchestrator.state import SessionStateUpdate
from repositories.sqlalchemy import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    SessionStateRepository,
    UserRepository,
)


@pytest.fixture
def session():
    """Create an in-memory SQLite session for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_personal_memory_skepticism_metadata(session):
    """Personal memory should store and retrieve skepticism metadata."""
    user_repo = UserRepository(session)
    user = user_repo.create(external_user_id="user_1")

    memory_repo = PersonalMemoryRepository(session)
    now = datetime.now(UTC)

    # Upsert with metadata
    memory = memory_repo.upsert(
        user_id=user.id,
        memory_key="pref_tabs",
        value={"tabs": 4},
        source="user_instruction",
        confidence=0.9,
        scope="global",
        last_verified_at=now,
        requires_verification=False,
    )

    assert memory.source == "user_instruction"
    assert memory.confidence == 0.9
    assert memory.scope == "global"
    assert memory.last_verified_at == now
    assert memory.requires_verification is False

    # Update value but keep/change metadata
    memory_updated = memory_repo.upsert(
        user_id=user.id,
        memory_key="pref_tabs",
        value={"tabs": 2},
        confidence=0.5,
        requires_verification=True,
    )

    assert memory_updated.value == {"tabs": 2}
    assert memory_updated.confidence == 0.5
    assert memory_updated.requires_verification is True


def test_project_memory_skepticism_metadata(session):
    """Project memory should store and retrieve skepticism metadata."""
    memory_repo = ProjectMemoryRepository(session)
    repo_url = "https://github.com/org/repo"

    memory = memory_repo.upsert(
        repo_url=repo_url,
        memory_key="build_cmd",
        value={"cmd": "make build"},
        source="repo_analysis",
        confidence=0.8,
    )

    assert memory.source == "repo_analysis"
    assert memory.confidence == 0.8
    assert memory.requires_verification is True  # Default


def test_session_state_repository_upsert_and_get(session):
    """SessionStateRepository should manage compact session context."""
    user_repo = UserRepository(session)
    user = user_repo.create(external_user_id="user_2")

    session_repo = SessionRepository(session)
    conv_session = session_repo.create(
        user_id=user.id, channel="test", external_thread_id="thread_1"
    )

    state_repo = SessionStateRepository(session)

    # Create
    state = state_repo.upsert(
        session_id=conv_session.id,
        active_goal="fix bug #123",
        decisions_made={"use_hooks": True},
        files_touched=["main.py"],
    )

    assert state.session_id == conv_session.id
    assert state.active_goal == "fix bug #123"
    assert state.decisions_made == {"use_hooks": True}
    assert state.files_touched == ["main.py"]

    # Update parts
    state_updated = state_repo.upsert(
        session_id=conv_session.id,
        active_goal="fix bug #123 and test",
        identified_risks={"timeout": "high"},
    )

    assert state_updated.active_goal == "fix bug #123 and test"
    assert state_updated.identified_risks == {"timeout": "high"}
    assert state_updated.files_touched == ["main.py"]  # Preserved
    assert state_updated.decisions_made == {"use_hooks": True}  # Preserved


def test_session_state_repository_merges_updates(session):
    """Session state updates should accumulate context instead of replacing it."""
    user_repo = UserRepository(session)
    user = user_repo.create(external_user_id="user_3")

    session_repo = SessionRepository(session)
    conv_session = session_repo.create(
        user_id=user.id, channel="test", external_thread_id="thread_2"
    )

    state_repo = SessionStateRepository(session)
    state_repo.upsert(
        session_id=conv_session.id,
        decisions_made={"worker": "codex", "format": "markdown"},
        identified_risks={"timeout": "medium"},
        files_touched=["orchestrator/graph.py", "workers/codex_worker.py"],
    )

    merged_state = state_repo.upsert(
        session_id=conv_session.id,
        decisions_made={"format": "plain", "retry": "allowed"},
        identified_risks={"network": "restricted"},
        files_touched=["workers/codex_worker.py", "repositories/sqlalchemy.py"],
    )

    assert merged_state.decisions_made == {
        "worker": "codex",
        "format": "plain",
        "retry": "allowed",
    }
    assert merged_state.identified_risks == {
        "timeout": "medium",
        "network": "restricted",
    }
    assert merged_state.files_touched == [
        "orchestrator/graph.py",
        "workers/codex_worker.py",
        "repositories/sqlalchemy.py",
    ]


def test_session_state_update_defaults_to_optional_fields():
    """SessionStateUpdate should preserve omitted fields as None for partial updates."""
    update = SessionStateUpdate(active_goal="Keep current context")

    assert update.decisions_made is None
    assert update.identified_risks is None
    assert update.files_touched is None
