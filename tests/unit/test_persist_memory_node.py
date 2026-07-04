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
    MemoryAdmissionDecisionRepository,
    MemoryProposalRepository,
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


def _admission_state(repo_url: str, verified_at: datetime) -> OrchestratorState:
    """Build a representative state with personal and project memory candidates."""
    return OrchestratorState.model_validate(
        {
            "session": {
                "session_id": "session-1",
                "user_id": "session-user",
                "channel": "http",
                "external_thread_id": "thread-1",
            },
            "task": {
                "task_id": "task-1",
                "task_text": "Persist memory",
                "repo_url": repo_url,
            },
            "result": {
                "status": "success",
                "summary": "done",
                "commands_run": [{"command": ".venv/bin/pytest tests/unit", "exit_code": 0}],
                "files_changed": ["tests/unit/test_persist_memory_node.py"],
                "test_results": [{"name": "unit", "status": "passed"}],
                "artifacts": [],
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
                    "confidence": 0.95,
                    "scope": "repo",
                    "requires_verification": True,
                },
            ],
        }
    )


def test_persist_memory_node_admits_personal_proposal_and_project_memory(
    session_factory,
) -> None:
    """DB-backed persist_memory should route candidates through admission."""
    repo_url = "https://github.com/natanayalo/code-agent"
    verified_at = datetime(2026, 7, 2, 10, 15, tzinfo=UTC)
    state = _admission_state(repo_url, verified_at)

    result = build_persist_memory_node(session_factory)(state)

    assert result["timeline_events"][0].event_type == TimelineEventType.MEMORY_PERSISTED
    assert result["timeline_events"][0].payload == {
        "requested_count": 2,
        "persisted_count": 1,
        "proposal_count": 1,
        "rejected_count": 0,
        "decision_counts": {"needs_human_review": 1, "create": 1},
        "risk_counts": {"medium": 1, "low": 1},
    }
    assert result["progress_updates"] == [
        "admitted 2 memory candidates: 1 direct writes, 1 proposals, 0 rejected"
    ]
    assert isinstance(result["memory_to_persist"][0]["last_verified_at"], str)

    with session_scope(session_factory) as session:
        personal = PersonalMemoryRepository(session).get(
            memory_key="communication_style",
        )
        project = ProjectMemoryRepository(session).get(
            repo_url=repo_url,
            memory_key="test_command",
        )
        proposals = MemoryProposalRepository(session).list(task_id="task-1")
        decisions = MemoryAdmissionDecisionRepository(session).list(task_id="task-1")

    assert personal is None
    assert proposals[0].memory_key == "communication_style"
    assert project is not None
    assert project.value == {"command": ".venv/bin/pytest tests/unit"}
    assert {decision.decision for decision in decisions} == {"create", "needs_human_review"}


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

    assert result["timeline_events"][0].payload["requested_count"] == 2
    assert result["timeline_events"][0].payload["persisted_count"] == 0
    assert result["timeline_events"][0].payload["proposal_count"] == 1
    assert result["timeline_events"][0].payload["rejected_count"] == 1

    with session_scope(session_factory) as session:
        personal = PersonalMemoryRepository(session).get(memory_key="communication_style")
        proposals = MemoryProposalRepository(session).list()
        decisions = MemoryAdmissionDecisionRepository(session).list()

    assert personal is None
    assert proposals[0].memory_key == "communication_style"
    assert {decision.decision for decision in decisions} == {"needs_human_review", "reject"}


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
        "proposal_count": 0,
        "rejected_count": 0,
        "decision_counts": {},
        "risk_counts": {},
    }
