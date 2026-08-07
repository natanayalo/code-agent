"""Additional unit tests for orchestrator/brain.py to reach high coverage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from db.enums import TimelineEventType
from orchestrator.brain import (
    RuleBasedOrchestratorBrain,
    _planner_failure_reason_code,
    extract_json_block,
)
from orchestrator.state import OrchestratorState, TaskRequest, TaskTimelineEventState
from tests.unit.orchestrator_brain_support import _ExplodingWorker, _StaticWorker
from workers import WorkerResult


def test_planner_failure_reason_code():
    assert _planner_failure_reason_code(None, TimeoutError("timed out")) == "timeout"
    assert _planner_failure_reason_code(None, TimeoutError()) == "timeout"
    assert _planner_failure_reason_code(None, RuntimeError("boom")) == "exception"
    assert _planner_failure_reason_code(None, None) == "unknown_error"

    res_timeout = WorkerResult(status="error", summary="Operation timed out after 30s")
    assert _planner_failure_reason_code(res_timeout) == "timeout"

    res_fk = WorkerResult(
        status="failure", summary="bad output", failure_kind="incomplete_delivery"
    )
    assert _planner_failure_reason_code(res_fk) == "incomplete_delivery"

    res_normal = WorkerResult(status="success", summary="done")
    assert _planner_failure_reason_code(res_normal) == "success"


def test_extract_json_block():
    assert extract_json_block("   ") == ""
    text_fenced = 'Some text before\n```json\n"nested string"\n```'
    assert extract_json_block(text_fenced) == '"nested string"'

    text_invalid_fence = "```json\n{invalid json\n```"
    # Fallback to balanced '{' finder
    assert extract_json_block(text_invalid_fence) == "```json\n{invalid json\n```"


@pytest.mark.anyio
async def test_brain_previous_attempts_history_and_planner_exception():
    now = datetime.now(UTC)
    events = [
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_DISPATCHED,
            message="dispatched",
            payload={"worker_type": "codex"},
            attempt_number=1,
            created_at=now,
        ),
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_FAILED,
            message="failed error",
            attempt_number=1,
            created_at=now,
        ),
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_DISPATCHED,
            message="dispatched2",
            payload={},
            attempt_number=2,
            created_at=now,
        ),
        TaskTimelineEventState(
            event_type=TimelineEventType.INFRA_FAILURE,
            message="infra error",
            attempt_number=2,
            created_at=now,
        ),
    ]

    task = TaskRequest(task_text="text")
    state = OrchestratorState(
        task=task,
        attempt_count=3,
        timeline_events=events,
    )

    from orchestrator.state import TaskSpec

    task_spec = TaskSpec(goal="text")
    workers = frozenset({"codex"})

    # 1. Planner explodes
    brain_exploding = RuleBasedOrchestratorBrain(planner_worker=_ExplodingWorker())
    res_exploding = await brain_exploding.suggest_task_spec_and_route(
        state=state, task_spec=task_spec, available_workers=workers
    )
    assert res_exploding is None

    # 2. Planner returns invalid json schema requiring tolerant coercion
    invalid_schema_payload = {
        "suggested_worker": "invalid_worker_name",
        "suggested_profile": 12345,
        "assumptions": ["assumption 1"],
    }
    brain_tolerant = RuleBasedOrchestratorBrain(
        planner_worker=_StaticWorker(
            WorkerResult(status="success", summary=None, json_payload=invalid_schema_payload)
        )
    )
    res_tolerant = await brain_tolerant.suggest_task_spec_and_route(
        state=state, task_spec=task_spec, available_workers=workers
    )
    assert res_tolerant is not None
    assert res_tolerant.assumptions == ["assumption 1"]
    assert res_tolerant.suggested_worker is None
