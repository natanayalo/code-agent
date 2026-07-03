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


class _FakeSession:
    def close(self) -> None:
        return None


class _FakeSessionStateRepository:
    def __init__(self, _session: object) -> None:
        pass

    def get(self, _session_id: str):
        return None


def _make_fake_search_result(memory_key: str):
    return type(
        "FakeSearchResult",
        (),
        {
            "memory": type(
                "MemoryRow",
                (),
                {
                    "memory_key": memory_key,
                    "value": {},
                    "source": None,
                    "confidence": 1.0,
                    "scope": None,
                    "last_verified_at": None,
                    "requires_verification": True,
                },
            )()
        },
    )()


def _patch_memory_repositories(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured_queries: list[tuple[str, str]],
    personal_key: str | None = None,
    project_key: str | None = None,
) -> None:
    class FakePersonalRepository:
        def __init__(self, _session: object) -> None:
            pass

        def search(self, *, user_id: str, query: str, limit: int):
            captured_queries.append(("personal", query))
            assert user_id == "user-1"
            assert limit == 20
            return [_make_fake_search_result(personal_key)] if personal_key else []

    class FakeProjectRepository:
        def __init__(self, _session: object) -> None:
            pass

        def search(self, *, repo_url: str, query: str, limit: int):
            captured_queries.append(("project", query))
            assert repo_url == "https://github.com/natanayalo/code-agent"
            assert limit == 20
            return [_make_fake_search_result(project_key)] if project_key else []

    monkeypatch.setattr("orchestrator.graph.PersonalMemoryRepository", FakePersonalRepository)
    monkeypatch.setattr("orchestrator.graph.ProjectMemoryRepository", FakeProjectRepository)
    monkeypatch.setattr(
        "orchestrator.graph.SessionStateRepository",
        _FakeSessionStateRepository,
    )


def _state_with_task(
    *,
    task_text: str,
    goal: str | None = None,
) -> OrchestratorState:
    payload: dict[str, Any] = {
        "session": {
            "session_id": "session-1",
            "user_id": "user-1",
            "channel": "http",
            "external_thread_id": "thread-1",
        },
        "task": {
            "task_text": task_text,
            "repo_url": "https://github.com/natanayalo/code-agent",
        },
    }
    if goal is not None:
        payload["task_spec"] = {
            "goal": goal,
            "assumptions": [],
            "acceptance_criteria": [],
            "non_goals": [],
            "risk_level": "low",
            "task_type": "feature",
            "allowed_actions": [],
            "forbidden_actions": [],
            "verification_commands": [],
            "expected_artifacts": [],
            "requires_clarification": False,
            "clarification_questions": [],
            "requires_permission": False,
            "delivery_mode": "summary",
        }
    return OrchestratorState.model_validate(payload)


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
        "retrieval_mode": "full_text",
        "search_query": "Use remembered test commands",
        "search_limit": 20,
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


def test_load_memory_node_prefers_task_spec_goal_for_search_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_queries: list[tuple[str, str]] = []
    _patch_memory_repositories(
        monkeypatch,
        captured_queries=captured_queries,
        personal_key="personal-memory",
        project_key="project-memory",
    )
    result = build_load_memory_node(lambda: _FakeSession())(
        _state_with_task(task_text="fallback task text", goal="preferred task goal")
    )

    assert captured_queries == [
        ("personal", "preferred task goal"),
        ("project", "preferred task goal"),
    ]
    assert result["timeline_events"][0].payload["search_query"] == "preferred task goal"
    assert result["memory"]["personal"][0]["memory_key"] == "personal-memory"
    assert result["memory"]["project"][0]["memory_key"] == "project-memory"


def test_load_memory_node_uses_task_text_when_task_spec_goal_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_queries: list[tuple[str, str]] = []
    _patch_memory_repositories(monkeypatch, captured_queries=captured_queries)
    result = build_load_memory_node(lambda: _FakeSession())(
        _state_with_task(task_text="use task text instead")
    )

    assert captured_queries == [
        ("personal", "use task text instead"),
        ("project", "use task text instead"),
    ]
    assert result["timeline_events"][0].payload["search_query"] == "use task text instead"


def test_load_memory_node_db_error_returns_empty_memory() -> None:
    """Memory loading should degrade gracefully if database access fails."""

    def broken_factory():
        raise RuntimeError("database unavailable")

    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})

    result = build_load_memory_node(broken_factory)(state)

    assert result["memory"] == {"personal": [], "project": [], "session": {}}
    assert result["timeline_events"][0].event_type == TimelineEventType.MEMORY_LOADED
