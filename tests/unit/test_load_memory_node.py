"""Unit tests for DB-backed orchestrator memory loading."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy.pool import StaticPool

import orchestrator.graph as graph_module
from db.base import Base
from db.enums import TimelineEventType
from orchestrator.graph import build_load_memory_node
from orchestrator.state import MemoryContext, MemoryEntry, OrchestratorState
from repositories import (
    ObservationRepository,
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
            memory_key="communication_style",
            value={"style": "concise", "shared_hint": "memory-match"},
            source="operator",
            confidence=0.9,
            scope="global",
            last_verified_at=verified_at,
            requires_verification=False,
        )
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="test_command",
            value={
                "command": ".venv/bin/pytest tests/unit",
                "shared_hint": "memory-match",
            },
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
        ObservationRepository(session).create(
            task_id="task-1",
            session_id=conversation.id,
            repo_url=repo_url,
            source="worker",
            event_type="worker_completed",
            summary="Previous worker completed.",
            content="content",
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

        def search(self, *, query: str, limit: int):
            captured_queries.append(("personal", query))
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
                "task_text": "memory-match",
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
    assert len(memory["observations"]) == 1
    assert memory["observations"][0]["summary"] == "Previous worker completed."

    event = result["timeline_events"][0]
    assert event.event_type == TimelineEventType.MEMORY_LOADED
    assert event.payload["observation_ids"] == [memory["observations"][0]["id"]]
    payload_without_observation_ids = dict(event.payload)
    payload_without_observation_ids.pop("observation_ids")
    assert payload_without_observation_ids == {
        "retrieval_mode": "full_text",
        "search_query": "memory-match",
        "search_limit": 20,
        "personal_count": 1,
        "project_count": 1,
        "observations_count": 1,
        "session_loaded": True,
        "personal_keys": ["communication_style"],
        "project_keys": ["test_command"],
        "loaded_count": 2,
        "accepted_count": 2,
        "suppressed_count": 0,
        "reason_counts": {},
        "accepted_keys": ["communication_style", "test_command"],
        "suppressed_keys": [],
        "suppressed_details": [],
    }


def test_load_memory_node_loads_personal_memory_without_session_scope(session_factory) -> None:
    """Missing session user should not prevent operator-global personal memory retrieval."""
    with session_scope(session_factory) as session:
        PersonalMemoryRepository(session).upsert(
            memory_key="operator_note",
            value={"hint": "hello"},
        )
    state = OrchestratorState.model_validate({"task": {"task_text": "hello"}})

    result = build_load_memory_node(session_factory)(state)

    assert result["memory"]["personal"][0]["memory_key"] == "operator_note"
    assert result["memory"]["project"] == []
    assert result["memory"]["session"] == {}
    assert result["timeline_events"][0].payload["personal_count"] == 1
    assert result["timeline_events"][0].payload["project_count"] == 0
    assert result["timeline_events"][0].payload["observations_count"] == 0


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

    assert result["memory"] == {
        "personal": [],
        "project": [],
        "session": {},
        "observations": [],
        "gate_diagnostics": None,
    }
    assert result["timeline_events"][0].event_type == TimelineEventType.MEMORY_LOADED
    assert result["timeline_events"][0].payload["observations_count"] == 0


def test_load_memory_node_records_span_input_output(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DB-backed load node should expose memory retrieval details to tracing."""
    user_id, session_id = _seed_memory_context(session_factory)
    captured: list[tuple[dict[str, Any], dict[str, Any]]] = []
    statuses: list[str] = []
    monkeypatch.setattr(graph_module, "start_optional_span", lambda **_kwargs: nullcontext())
    monkeypatch.setattr(
        graph_module,
        "set_span_input_output",
        lambda input_data, output_data=None: captured.append((input_data, output_data)),
    )
    monkeypatch.setattr(
        graph_module,
        "set_span_status_from_outcome",
        lambda status, *_args, **_kwargs: statuses.append(status),
    )
    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": session_id,
                "user_id": user_id,
                "channel": "http",
                "external_thread_id": "thread-1",
            },
            "task": {
                "task_text": "memory-match",
                "repo_url": "https://github.com/natanayalo/code-agent",
            },
        }
    )

    build_load_memory_node(session_factory)(state)

    assert captured[0][0]["source"] == "database"
    assert captured[0][0]["search_query"] == "memory-match"
    assert captured[0][1]["personal_count"] == 1
    assert captured[0][1]["project_count"] == 1
    assert captured[0][1]["observations_count"] == 1
    assert statuses == ["success"]


def test_load_memory_node_gating_and_deduplication(session_factory) -> None:
    """Test that the read-side gate filters cross-scope conflicts and resolves duplicates."""
    from datetime import UTC, datetime

    repo_url = "https://github.com/natanayalo/code-agent"

    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="gate-user")
        conv = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-gate",
        )

        # 1. Seed cross-scope conflict:
        # Personal memory has 'style' key, Project memory also has 'style' key.
        # Project should override personal.
        PersonalMemoryRepository(session).upsert(
            memory_key="style",
            value={"type": "personal"},
            source="user",
            confidence=1.0,
            scope="global",
            last_verified_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="style",
            value={"type": "project"},
            source="worker",
            confidence=1.0,
            scope="repo",
            last_verified_at=datetime(2026, 7, 2, tzinfo=UTC),
        )

        session.flush()
        user_id = user.id
        session_id = conv.id

    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": session_id,
                "user_id": user_id,
                "channel": "http",
                "external_thread_id": "thread-gate",
            },
            "task": {
                "task_text": "style",
                "repo_url": repo_url,
            },
        }
    )

    node_result = build_load_memory_node(session_factory)(state)
    loaded_memory = node_result["memory"]

    # Personal memory for 'style' should be filtered out
    assert len(loaded_memory["personal"]) == 0

    # Project memory for 'style' should be kept
    assert len(loaded_memory["project"]) == 1
    assert loaded_memory["project"][0]["memory_key"] == "style"
    assert loaded_memory["project"][0]["value"] == {"type": "project"}


def test_apply_read_side_gate_handles_none_confidence() -> None:
    """Test that read-side gating tolerates None confidence values during dedupe."""
    memory = MemoryContext.model_construct(
        personal=[
            MemoryEntry.model_construct(
                memory_key="style",
                value={"type": "personal"},
                confidence=None,
                last_verified_at=datetime(2026, 7, 1, tzinfo=UTC),
            ),
            MemoryEntry.model_construct(
                memory_key="style",
                value={"type": "personal-preferred"},
                confidence=1.1,
                last_verified_at=datetime(2026, 7, 1, tzinfo=UTC),
            ),
        ],
        project=[],
        session={},
        observations=[],
    )

    gated = graph_module._apply_read_side_gate(memory)

    assert len(gated.personal) == 1
    assert gated.personal[0].value == {"type": "personal-preferred"}


def test_apply_read_side_gate_normalizes_timezone_aware_verified_at() -> None:
    """Test that timezone-aware verified timestamps are compared in UTC correctly."""
    memory = MemoryContext.model_construct(
        personal=[
            MemoryEntry.model_construct(
                memory_key="style",
                value={"type": "old"},
                confidence=0.8,
                last_verified_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))),
            ),
            MemoryEntry.model_construct(
                memory_key="style",
                value={"type": "new"},
                confidence=0.8,
                last_verified_at=datetime(2026, 7, 1, 11, 30, tzinfo=UTC),
            ),
        ],
        project=[],
        session={},
        observations=[],
    )

    gated = graph_module._apply_read_side_gate(memory)

    assert len(gated.personal) == 1
    assert gated.personal[0].value == {"type": "new"}


def test_read_side_gate_does_not_mutate_db(session_factory) -> None:
    """Verify that read-side gating does not mutate memory tables in the DB."""
    from db.models import PersonalMemory

    repo_url = "https://github.com/natanayalo/code-agent"

    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="gate-user")
        conv = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-gate-mute",
        )
        # Seed a key that would trigger conflict/override
        PersonalMemoryRepository(session).upsert(
            memory_key="api_token",
            value={"token": "secret"},
            requires_verification=True,
        )
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="api_token",
            value={"token": "different"},
            requires_verification=True,
        )
        session.flush()
        user_id = user.id
        session_id = conv.id

    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": session_id,
                "user_id": user_id,
                "channel": "http",
                "external_thread_id": "thread-gate-mute",
            },
            "task": {
                "task_text": "api_token",
                "repo_url": repo_url,
            },
        }
    )

    # Execute memory load which triggers gating
    node_result = build_load_memory_node(session_factory)(state)

    # Verify memory is annotated in state
    loaded_memory = node_result["memory"]
    assert len(loaded_memory["personal"]) == 0  # suppressed due to conflict + high risk
    assert len(loaded_memory["project"]) == 0  # suppressed due to high risk + unverified

    # Check that personal memory in DB remains exactly as it was
    with session_scope(session_factory) as session:
        db_personal = session.query(PersonalMemory).filter_by(memory_key="api_token").one()
        # Ensure requires_verification is still True, value is unchanged, etc.
        assert db_personal.requires_verification is True
        assert db_personal.value == {"token": "secret"}


def test_read_side_gate_staleness_and_advisory_strength() -> None:
    """Test staleness windows and strength calculations based on key risk/type."""
    from memory.read_side_gate import ReadSideMemoryGateService, _calculate_staleness

    now = datetime.now(UTC)
    # Stale known_pitfalls (> 60 days)
    entry_pitfall = MemoryEntry(
        memory_key="known_pitfalls",
        value={"tip": "do not crash"},
        confidence=0.9,
        last_verified_at=now - timedelta(days=70),
        requires_verification=False,
    )

    # Fresh deploy instructions (< 14 days)
    entry_deploy = MemoryEntry(
        memory_key="deploy_policy",
        value={"cmd": "deploy"},
        confidence=0.8,
        last_verified_at=now - timedelta(days=5),
        requires_verification=False,
    )

    entry_unverified_deploy = MemoryEntry(
        memory_key="deploy_policy_without_timestamp",
        value={"cmd": "old deploy flow"},
        confidence=0.8,
        last_verified_at=None,
        requires_verification=False,
    )

    result = ReadSideMemoryGateService.process(
        personal=[entry_pitfall, entry_deploy, entry_unverified_deploy],
        project=[],
    )

    # Pitfall is stale (staleness = 1.0 because 70 > 60)
    # deploy is fresh (staleness = 5 / 14 = ~0.357)
    pitfall_ann = next(e for e in result.accepted_personal if e.memory_key == "known_pitfalls")
    deploy_ann = next(e for e in result.accepted_personal if e.memory_key == "deploy_policy")

    assert pitfall_ann.staleness == 1.0
    # strength = 0.9 * (1.0 - 1.0 * 0.5) = 0.45
    assert pytest.approx(pitfall_ann.advisory_strength) == 0.45
    assert pitfall_ann.gate_status == "advisory"  # because staleness >= 0.5

    assert pytest.approx(deploy_ann.staleness, abs=0.01) == 0.357
    # strength = 0.8 * (1.0 - 0.357 * 0.5) = 0.657
    assert pytest.approx(deploy_ann.advisory_strength, abs=0.01) == 0.657
    assert deploy_ann.gate_status == "accepted"

    assert not any(
        e.memory_key == "deploy_policy_without_timestamp" for e in result.accepted_personal
    )
    suppressed_deploy = next(
        e for e in result.suppressed_personal if e.memory_key == "deploy_policy_without_timestamp"
    )
    assert suppressed_deploy.reason_codes == ["high_risk_unverified_or_stale"]

    assert _calculate_staleness(None, requires_verification=False, window_days=30) == 1.0
    assert _calculate_staleness(now, requires_verification=False, window_days=0) == 1.0
    assert _calculate_staleness(now, requires_verification=False, window_days=-1) == 1.0


def test_read_side_gate_conflict_coexistence_for_collections() -> None:
    """Test same-key conflict logic for scalar keys vs collection keys."""
    from memory.read_side_gate import ReadSideMemoryGateService

    now = datetime.now(UTC)

    # Collection key: 'verification_commands'
    entry_coll_personal = MemoryEntry(
        memory_key="verification_commands",
        value={"unit": "pytest"},
        confidence=0.9,
        requires_verification=False,
        last_verified_at=now,
    )
    entry_coll_project = MemoryEntry(
        memory_key="verification_commands",
        value={"integration": "pytest-integration"},
        confidence=0.9,
        requires_verification=False,
        last_verified_at=now,
    )

    # Scalar key: 'editor' (different values)
    entry_scal_personal = MemoryEntry(
        memory_key="editor",
        value={"theme": "light"},
        confidence=0.9,
        requires_verification=False,
        last_verified_at=now,
    )
    entry_scal_project = MemoryEntry(
        memory_key="editor",
        value={"theme": "dark"},
        confidence=0.9,
        requires_verification=False,
        last_verified_at=now,
    )

    result = ReadSideMemoryGateService.process(
        personal=[entry_coll_personal, entry_scal_personal],
        project=[entry_coll_project, entry_scal_project],
    )

    # Collections coexist without contradiction -> both accepted
    assert any(e.memory_key == "verification_commands" for e in result.accepted_personal)
    assert any(e.memory_key == "verification_commands" for e in result.accepted_project)

    # Scalar is overridden -> personal suppressed, project kept
    assert not any(e.memory_key == "editor" for e in result.accepted_personal)
    editor_proj = next(e for e in result.accepted_project if e.memory_key == "editor")
    assert editor_proj.conflict == "personal_conflict_resolved"
    # Project gets 0.8 conflict penalty: strength = 0.9 * 0.8 = 0.72
    assert pytest.approx(editor_proj.advisory_strength) == 0.72


def test_read_side_gate_diagnostics_timeline_payload(session_factory) -> None:
    """Verify that MEMORY_LOADED event includes correct gating diagnostics."""
    repo_url = "https://github.com/natanayalo/code-agent"
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="gate-user-diag")
        conv = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="thread-gate-diag",
        )
        # Seed 1 accepted project memory, 1 suppressed personal (due to conflict)
        PersonalMemoryRepository(session).upsert(
            memory_key="editor",
            value={"theme": "light"},
        )
        ProjectMemoryRepository(session).upsert(
            repo_url=repo_url,
            memory_key="editor",
            value={"theme": "dark"},
        )
        session.flush()
        user_id = user.id
        session_id = conv.id

    state = OrchestratorState.model_validate(
        {
            "session": {
                "session_id": session_id,
                "user_id": user_id,
                "channel": "http",
                "external_thread_id": "thread-gate-diag",
            },
            "task": {
                "task_text": "editor",
                "repo_url": repo_url,
            },
        }
    )

    node_result = build_load_memory_node(session_factory)(state)
    timeline_events = node_result.get("timeline_events", [])
    memory_event = next(e for e in timeline_events if e.event_type == "memory_loaded")
    payload = memory_event.payload

    assert payload["loaded_count"] == 2
    assert payload["accepted_count"] == 1
    assert payload["suppressed_count"] == 1
    assert "project_overrides_personal" in payload["reason_counts"]
    assert "editor" in payload["accepted_keys"]
    assert "editor" in payload["suppressed_keys"]


def test_determine_risk_high_risk_substrings() -> None:
    """Verify that _determine_risk identifies high risk keys within larger names."""
    from memory.read_side_gate import _determine_risk

    assert _determine_risk("my_api_key", False) == "high"
    assert _determine_risk("aws_access_key", False) == "high"
    assert _determine_risk("custom_private_key", False) == "high"
    assert _determine_risk("some_deploy_action", False) == "high"
    assert _determine_risk("auth_token", False) == "high"
    assert _determine_risk("non_risk_key", False) == "low"
    assert _determine_risk("non_risk_key", True) == "medium"
