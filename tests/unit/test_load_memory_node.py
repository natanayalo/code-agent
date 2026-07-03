"""Unit tests for DB-backed orchestrator memory loading."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TimelineEventType
from orchestrator.graph import build_load_memory_node
from orchestrator.state import OrchestratorState
from repositories import (
    PersonalMemoryRepository,
    ProjectMemoryRepository,
    SessionRepository,
    SessionStateRepository,
    UserRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


@pytest.fixture
def session_factory():
    """Create an in-memory SQLite session factory for memory-node tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_memory_context(session_factory: Any) -> tuple[str, str]:
    verified_at = datetime(2026, 7, 2, 9, 30, tzinfo=UTC)
    repo_url = "https://github.com/natanayalo/code-agent"

    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="memory-node-user")
        conversation = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-1",
        )
        PersonalMemoryRepository(session).upsert(
            user_id=user.id,
            memory_key="communication_style",
            value={"style": "concise"},
            source="operator",
            confidence=0.9,
            scope="global",
            last_verified_at=verified_at,
            requires_verification=False,
        )
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="test_command",
            value={"command": ".venv/bin/pytest tests/unit"},
            source="worker_result",
            confidence=0.8,
            scope="repo",
            last_verified_at=verified_at,
            requires_verification=True,
        )
        SessionStateRepository(session).upsert(
            session_id=conversation.id,
            active_goal="wire memory into worker context",
            decisions_made={"load_strategy": "all"},
            identified_risks={"prompt_size": "small personal-use volumes"},
            files_touched=["orchestrator/graph.py"],
        )
        return user.id, conversation.id


def test_load_memory_node_loads_memory_and_skepticism_metadata(session_factory) -> None:
    """DB-backed load_memory should populate personal, project, and session memory."""
    user_id, session_id = _seed_memory_context(session_factory)
    repo_url = "https://github.com/natanayalo/code-agent"
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": session_id,
                "user_id": user_id,
                "channel": "http",
                "external_thread_id": "thread-1",
            },
            "task": {
                "task_text": "Use remembered test commands",
                "repo_url": repo_url,
            },
        }
    )

    result = build_load_memory_node(session_factory)(state)

    memory = result["memory"]
    assert memory["personal"][0]["memory_key"] == "communication_style"
    assert memory["personal"][0]["source"] == "operator"
    assert memory["personal"][0]["confidence"] == 0.9
    assert memory["personal"][0]["requires_verification"] is False
    assert isinstance(memory["personal"][0]["last_verified_at"], str)
    assert memory["project"][0]["memory_key"] == "test_command"
    assert memory["session"]["active_goal"] == "wire memory into worker context"
    assert memory["session"]["files_touched"] == ["orchestrator/graph.py"]

    event = result["timeline_events"][0]
    assert event.event_type == TimelineEventType.MEMORY_LOADED
    assert event.payload == {
        "retrieval_mode": "load_all",
        "personal_count": 1,
        "project_count": 1,
        "session_loaded": True,
        "personal_keys": ["communication_style"],
        "project_keys": ["test_command"],
    }


def test_load_memory_node_skips_missing_scopes(session_factory) -> None:
    """Missing user/session and repo scope should simply produce empty memory."""
    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})

    result = build_load_memory_node(session_factory)(state)

    assert result["memory"] == {"personal": [], "project": [], "session": {}}
    assert result["timeline_events"][0].payload["personal_count"] == 0
    assert result["timeline_events"][0].payload["project_count"] == 0


def test_load_memory_node_db_error_returns_empty_memory() -> None:
    """Memory loading should degrade gracefully if database access fails."""

    def broken_factory():
        raise RuntimeError("database unavailable")

    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})

    result = build_load_memory_node(broken_factory)(state)

    assert result["memory"] == {"personal": [], "project": [], "session": {}}
    assert result["timeline_events"][0].event_type == TimelineEventType.MEMORY_LOADED
