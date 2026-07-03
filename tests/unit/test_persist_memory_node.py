"""Unit tests for DB-backed orchestrator memory persistence."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TimelineEventType
from orchestrator.graph import build_persist_memory_node
from orchestrator.state import OrchestratorState
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


@pytest.fixture
def session_factory():
    """Create an in-memory SQLite session factory for persist-node tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_persist_memory_node_persists_personal_and_project_memory(session_factory) -> None:
    """DB-backed persist_memory should route personal and project entries correctly."""
    repo_url = "https://github.com/natanayalo/code-agent"
    verified_at = datetime(2026, 7, 2, 10, 15, tzinfo=UTC)

    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "session-1",
                "user_id": "session-user",
                "channel": "http",
                "external_thread_id": "thread-1",
            },
            "task": {
                "task_text": "Persist memory",
                "repo_url": repo_url,
            },
            "memory_to_persist": [
                {
                    "category": "personal",
                    "memory_key": "communication_style",
                    "value": {"style": "concise"},
                    "source": "worker_result",
                    "confidence": 0.7,
                    "scope": "global",
                    "last_verified_at": verified_at.isoformat(),
                    "requires_verification": False,
                },
                {
                    "category": "project",
                    "memory_key": "test_command",
                    "value": {"command": ".venv/bin/pytest tests/unit"},
                    "source": "worker_result",
                    "confidence": 0.8,
                    "scope": "repo",
                    "requires_verification": True,
                },
            ],
        }
    )

    result = build_persist_memory_node(session_factory)(state)

    assert result["timeline_events"][0].event_type == TimelineEventType.MEMORY_PERSISTED
    assert result["timeline_events"][0].payload == {
        "requested_count": 2,
        "persisted_count": 2,
    }
    assert result["progress_updates"] == ["persisted 2 memory entries"]
    assert isinstance(result["memory_to_persist"][0]["last_verified_at"], str)

    with session_scope(session_factory) as session:
        personal = PersonalMemoryRepository(session).get(
            memory_key="communication_style",
        )
        project = ProjectMemoryRepository(session).get(
            repo_url=repo_url,
            memory_key="test_command",
        )

    assert personal is not None
    assert personal.value == {"style": "concise"}
    assert personal.source == "worker_result"
    assert personal.requires_verification is False
    assert project is not None
    assert project.value == {"command": ".venv/bin/pytest tests/unit"}


def test_persist_memory_node_persists_personal_without_session_scope(session_factory) -> None:
    """Personal memory is operator-global, while project memory still needs a repo URL."""
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Persist memory without scopes"},
            "memory_to_persist": [
                {
                    "category": "personal",
                    "memory_key": "communication_style",
                    "value": {"style": "concise"},
                },
                {
                    "category": "project",
                    "memory_key": "test_command",
                    "value": {"command": "pytest"},
                },
            ],
        }
    )

    result = build_persist_memory_node(session_factory)(state)

    assert result["timeline_events"][0].payload == {
        "requested_count": 2,
        "persisted_count": 1,
    }

    with session_scope(session_factory) as session:
        personal = PersonalMemoryRepository(session).get(memory_key="communication_style")
        assert personal is not None
        assert personal.value == {"style": "concise"}


def test_persist_memory_node_db_error_does_not_crash() -> None:
    """Memory persistence should degrade gracefully if database access fails."""

    def broken_factory():
        raise RuntimeError("database unavailable")

    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Persist memory"},
            "memory_to_persist": [
                {
                    "category": "project",
                    "repo_url": "https://github.com/natanayalo/code-agent",
                    "memory_key": "test_command",
                    "value": {"command": "pytest"},
                }
            ],
        }
    )

    result = build_persist_memory_node(broken_factory)(state)

    assert result["timeline_events"][0].event_type == TimelineEventType.MEMORY_PERSISTED
    assert result["timeline_events"][0].payload == {
        "requested_count": 1,
        "persisted_count": 0,
    }
