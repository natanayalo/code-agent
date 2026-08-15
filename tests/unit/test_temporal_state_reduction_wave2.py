"""Unit and behavioral equivalence tests for M28.5B Wave 2 state reduction closeout.

Covers the candidate fields pruned from intermediate TemporalTaskState snapshots:
- friction_reports
- errors
- session_state_update
- scout_phase_results
- memory_to_persist
"""

from __future__ import annotations

import types
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from temporalio import workflow

from db.base import Base
from db.enums import TimelineEventType
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.graph import _resolve_memory_to_persist
from orchestrator.reflection import FrictionReport
from orchestrator.state import (
    OrchestratorState,
    PersistMemoryEntry,
    ScoutPhaseResult,
    SessionRef,
    SessionStateUpdate,
    TaskRequest,
    TaskSpec,
)
from orchestrator.temporal.activities import (
    EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS,
    TaskExecutionActivities,
    _serialize_temporal_task_state,
)
from orchestrator.temporal.workflows import TaskExecutionWorkflow
from repositories import (
    ProjectMemoryRepository,
    ProposalRepository,
    SessionStateRepository,
    TaskRepository,
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    session_scope,
)
from workers import (
    WorkerCommand,
    WorkerMemoryEntry,
    WorkerResult,
    WorkerTestResult,
)


def _make_db() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _make_activities(factory: sessionmaker[Session]) -> TaskExecutionActivities:
    svc = MagicMock()
    svc.worker = MagicMock()
    svc.worker_profiles = {}
    svc.enable_worker_profiles = False
    svc.enable_independent_verifier = False
    svc.session_factory = factory
    svc.workspace_manager = None
    svc.retention_seconds = None
    svc.orchestrator_brain = None
    svc.progress_notifier = None
    svc._persist_execution_outcome = types.MethodType(_persist_execution_outcome, svc)

    async def _async_run_blocking(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    svc._run_blocking = _async_run_blocking
    return TaskExecutionActivities(svc)


def _make_sample_state(
    task_id: str = "task-w2-1",
    session_id: str = "session-w2-1",
) -> OrchestratorState:
    return OrchestratorState(
        task=TaskRequest(
            task_id=task_id,
            repo_url="https://github.com/example/repo",
            task_text="Run test task",
        ),
        session=SessionRef(
            session_id=session_id,
            user_id="user-1",
            channel="api",
            external_thread_id="thread-1",
        ),
        current_step="generate_task_spec_and_route",
        attempt_count=1,
    )


# ---------------------------------------------------------------------------
# 1. Serialization & Deserialization Defaults
# ---------------------------------------------------------------------------


def test_wave_2_remaining_fields_in_excluded_set() -> None:
    """All Wave 2 candidate fields must be present in EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS."""
    expected = {
        "progress_updates",
        "timeline_events",
        "friction_reports",
        "errors",
        "session_state_update",
        "scout_phase_results",
        "memory_to_persist",
    }
    assert expected.issubset(EXCLUDED_TEMPORAL_SNAPSHOT_FIELDS)


def test_wave_2_remaining_fields_serialization_and_defaults() -> None:
    """_serialize_temporal_task_state strips all remaining Wave 2 fields and defaults on reload."""
    state = _make_sample_state()
    state.friction_reports = [
        FrictionReport(
            source="tooling",
            description="test failed",
            impact="slowed_down",
        )
    ]
    state.errors = ["task_spec_policy: blocked"]
    state.session_state_update = SessionStateUpdate(active_goal="build feature")
    state.scout_phase_results = [
        ScoutPhaseResult(
            phase="repo",
            result=WorkerResult(status="success", summary="repo analyzed"),
        )
    ]
    state.memory_to_persist = [
        PersistMemoryEntry(
            category="project",
            memory_key="build_tool",
            value={"content": "poetry"},
        )
    ]

    serialized = _serialize_temporal_task_state(state)

    for field in (
        "friction_reports",
        "errors",
        "session_state_update",
        "scout_phase_results",
        "memory_to_persist",
    ):
        assert field not in serialized

    reloaded = OrchestratorState.model_validate(serialized)
    assert reloaded.friction_reports == []
    assert reloaded.errors == []
    assert reloaded.session_state_update is None
    assert reloaded.scout_phase_results == []
    assert reloaded.memory_to_persist == []


# ---------------------------------------------------------------------------
# 2. Memory Canonical Resolution & Behavioral Equivalence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wave_2_memory_canonical_resolution_and_admission() -> None:
    """Canonical memory admission resolves from result when state.memory_to_persist is pruned."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-mem-1",
            task_text="Run memory admission test",
            repo_url="https://github.com/example/repo",
        )
        task_id = task.id

    state = _make_sample_state(task_id=task_id, session_id="session-mem-1")
    state.result = WorkerResult(
        status="success",
        summary="task done",
        commands_run=[WorkerCommand(command=".venv/bin/pytest tests/unit", exit_code=0)],
        files_changed=["tests/unit/test_foo.py"],
        test_results=[WorkerTestResult(name="unit", status="passed")],
        memory_to_persist=[
            WorkerMemoryEntry(
                category="project",
                memory_key="test_command",
                value={"command": ".venv/bin/pytest tests/unit"},
                source="worker_result",
                confidence=0.95,
                scope="repo",
                requires_verification=True,
            )
        ],
    )
    # Intermediate state serialization strips memory_to_persist
    state.memory_to_persist = []

    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id,
            state=_serialize_temporal_task_state(state),
        )

    await activities.persist_memory(task_id)

    with session_scope(factory) as session:
        stored_memory = ProjectMemoryRepository(session).list_by_repo(
            repo_url="https://github.com/example/repo"
        )
        assert len(stored_memory) == 1
        assert stored_memory[0].memory_key == "test_command"
        assert stored_memory[0].value == {"command": ".venv/bin/pytest tests/unit"}

        timeline_events = TaskTimelineRepository(session).list_by_task(task_id)
        mem_events = [
            e for e in timeline_events if e.event_type == TimelineEventType.MEMORY_PERSISTED
        ]
        assert len(mem_events) == 1
        assert mem_events[0].payload.get("persisted_count") == 1
        assert mem_events[0].payload.get("requested_count") == 1


def test_wave_2_memory_rolling_legacy_no_double_admission() -> None:
    """_resolve_memory_to_persist prefers state.memory_to_persist and avoids double mapping."""
    state = _make_sample_state()
    state.memory_to_persist = [
        PersistMemoryEntry(
            category="project",
            memory_key="existing_key",
            value={"content": "val1"},
        )
    ]
    state.result = WorkerResult(
        status="success",
        memory_to_persist=[
            WorkerMemoryEntry(
                category="project",
                memory_key="existing_key",
                value={"content": "val1"},
            )
        ],
    )

    resolved = _resolve_memory_to_persist(state)
    assert len(resolved) == 1
    assert resolved[0].memory_key == "existing_key"


# ---------------------------------------------------------------------------
# 3. Session State Regenerated-at-Consumption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wave_2_session_state_update_regenerated_at_consumption() -> None:
    """deliver_result regenerates session state update on consumption without snapshot."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-ses-1",
            task_text="Run delivery test",
        )
        task_id = task.id

    state = _make_sample_state(task_id=task_id, session_id="session-ses-1")
    state.session_state_update = None
    state.result = WorkerResult(
        status="success",
        summary="completed work",
        files_changed=["src/main.py"],
    )

    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id,
            state=_serialize_temporal_task_state(state),
        )

    await activities.deliver_result(task_id)

    with session_scope(factory) as session:
        session_state = SessionStateRepository(session).get("session-ses-1")
        assert session_state is not None
        assert session_state.files_touched == ["src/main.py"]


# ---------------------------------------------------------------------------
# 4. Errors Terminal Projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wave_2_errors_pruned_and_terminal_failure_projected() -> None:
    """Exhausted workflow failure correctly projects tasks.last_error with pruned errors."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-err-1",
            task_text="Run error test",
        )
        task_id = task.id

    state = _make_sample_state(task_id=task_id, session_id="session-err-1")
    state.errors = ["intermediate error"]

    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id,
            state=_serialize_temporal_task_state(state),
        )

    await activities.record_workflow_failure(task_id, "Fatal workflow timeout")

    with session_scope(factory) as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert "Fatal workflow timeout" in (task.last_error or "")

        events = TaskTimelineRepository(session).list_by_task(task_id)
        failed_events = [e for e in events if e.event_type == TimelineEventType.TASK_FAILED]
        assert len(failed_events) == 1

        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is None


# ---------------------------------------------------------------------------
# 5. Friction Reports Ephemeral Discard Policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wave_2_friction_reports_discard_policy() -> None:
    """Verification friction is ephemeral in Temporal without disrupting terminal delivery."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-fric-1",
            task_text="Run friction test",
        )
        task_id = task.id

    state = _make_sample_state(task_id=task_id, session_id="session-fric-1")
    state.friction_reports = [
        FrictionReport(
            source="tooling",
            description="assertion diff",
            impact="slowed_down",
        )
    ]
    state.result = WorkerResult(status="success", summary="all tests passed")

    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id,
            state=_serialize_temporal_task_state(state),
        )

    await activities.deliver_result(task_id)

    with session_scope(factory) as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is None


# ---------------------------------------------------------------------------
# 6. Scout Deep Mode Behavioral Equivalence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wave_2_scout_deep_mode_temporal_behavioral_equivalence() -> None:
    """Temporal scout execution in deep mode persists proposals without scout_phase_results."""
    factory = _make_db()
    activities = _make_activities(factory)

    with session_scope(factory) as session:
        task = TaskRepository(session).create(
            session_id="session-scout-1",
            task_text="Perform deep codebase exploration",
            constraints={"scout_mode": "deep", "max_proposals": 5},
        )
        task_id = task.id

    state = _make_sample_state(task_id=task_id, session_id="session-scout-1")
    state.task_kind = "scout"
    state.task_spec = TaskSpec(
        task_type="scout",
        goal="Perform deep codebase exploration",
    )
    state.result = WorkerResult(
        status="success",
        summary="Exploration finished with proposal",
        artifacts=[],
        commands_run=[],
        json_payload={
            "proposals": [
                {
                    "title": "Refactor Architecture",
                    "description": "Simplify module boundaries",
                    "value": "high",
                    "effort": "medium",
                    "risk": "low",
                    "layer_impact": "orchestrator",
                    "validation_path": "pytest tests/unit",
                    "hitl_need": "none",
                    "evidence": ["modular separation reduces complexity"],
                    "implementation_slice": "Slice 1: update interfaces",
                }
            ]
        },
    )

    with session_scope(factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=task_id,
            state=_serialize_temporal_task_state(state),
        )

    # Deliver result for scout task
    await activities.deliver_result(task_id)

    with session_scope(factory) as session:
        snapshot = TemporalTaskStateRepository(session).get(task_id=task_id)
        assert snapshot is None
        task = TaskRepository(session).get(task_id)
        assert task is not None
        proposals = ProposalRepository(session).list_proposals(task_id=task_id)
        assert len(proposals) == 1
        assert proposals[0].title == "Refactor Architecture"


@pytest.mark.anyio
async def test_wave_2_scout_deep_mode_workflow_executes_single_worker_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deep scout in Temporal executes exactly one worker phase without research chaining."""
    activity_names: list[str] = []

    async def execute_activity(name: str, *args: Any, **kwargs: Any) -> Any:
        activity_names.append(name)
        if name == "classify_and_plan":
            return {}
        if name == "decompose_task":
            return {"execution_shape": "monolithic"}
        if name == "run_worker":
            return {}
        return None

    monkeypatch.setattr(workflow, "patched", lambda _patch_id: False)
    monkeypatch.setattr(workflow, "execute_activity", execute_activity)

    await TaskExecutionWorkflow()._run_lifecycle("task-scout-deep-1")

    assert activity_names.count("run_worker") == 1
    assert "transition_to_research_phase" not in activity_names
    assert activity_names == [
        "classify_and_plan",
        "decompose_task",
        "load_memory",
        "provision_workspace",
        "run_worker",
        "verify_result",
        "persist_memory",
        "deliver_result",
    ]
