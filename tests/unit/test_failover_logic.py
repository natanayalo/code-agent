import asyncio
from typing import Any

import pytest

from db.enums import TimelineEventType
from orchestrator.brain import RuleBasedOrchestratorBrain
from orchestrator.graph import _compute_legacy_route_decision
from orchestrator.state import (
    OrchestratorState,
    TaskTimelineEventState,
)
from workers import Worker, WorkerRequest, WorkerResult


class MockWorker(Worker):
    def __init__(self, result: WorkerResult | None = None, side_effect: Any = None):
        self.result = result
        self.side_effect = side_effect
        self.run_count = 0

    async def run(self, request: WorkerRequest, system_prompt: str | None = None) -> WorkerResult:
        self.run_count += 1
        if self.side_effect:
            if asyncio.iscoroutinefunction(self.side_effect):
                return await self.side_effect(request)
            return self.side_effect(request)
        if self.result is None:
            raise RuntimeError("MockWorker has no result or side_effect")
        return self.result


def _state(
    attempt_count: int = 0,
    timeline_events: list[TaskTimelineEventState] | None = None,
) -> OrchestratorState:
    return OrchestratorState.model_validate(
        {
            "task": {
                "task_text": "test task",
                "repo_url": "url",
                "branch": "main",
            },
            "task_kind": "implementation",
            "attempt_count": attempt_count,
            "timeline_events": timeline_events or [],
            "dispatch": {"worker_type": "gemini"} if attempt_count > 0 else {},
            "result": (
                {"status": "failure", "failure_kind": "provider_error"}
                if attempt_count > 0
                else None
            ),
        }
    )


@pytest.mark.asyncio
async def test_brain_fallback_to_secondary_planner():
    """Verify that the brain attempts fallback planners if the primary one fails."""
    primary = MockWorker(
        result=WorkerResult(
            status="failure", failure_kind="provider_error", summary="429 Exhausted"
        )
    )
    secondary = MockWorker(
        result=WorkerResult(
            status="success", summary='{"suggested_worker":"codex", "rationale":"fallback"}'
        )
    )

    brain = RuleBasedOrchestratorBrain(planner_worker=primary, fallback_planners=[secondary])

    state = _state()
    suggestion = await brain.suggest_route(
        state=state,
        available_workers=frozenset({"codex", "gemini"}),
        available_profiles=None,
    )

    assert suggestion is not None
    assert suggestion.suggested_worker == "codex"
    assert suggestion.rationale == "fallback"
    assert primary.run_count == 1
    assert secondary.run_count == 1


@pytest.mark.asyncio
async def test_brain_skips_all_if_all_planners_fail():
    """Verify that the brain returns None if all configured planners fail."""
    primary = MockWorker(
        result=WorkerResult(status="failure", failure_kind="provider_error", summary="429")
    )
    secondary = MockWorker(result=WorkerResult(status="error", summary="crashed"))

    brain = RuleBasedOrchestratorBrain(planner_worker=primary, fallback_planners=[secondary])

    state = _state()
    suggestion = await brain.suggest_route(
        state=state,
        available_workers=frozenset({"codex", "gemini"}),
    )

    assert suggestion is None
    assert primary.run_count == 1
    assert secondary.run_count == 1


def test_legacy_rotation_avoids_previously_failed_workers():
    """Verify that deterministic rotation skips workers that have already failed."""
    # Attempt 0: Gemini failed
    events = [
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_DISPATCHED,
            attempt_number=0,
            payload={"worker_type": "gemini"},
        ),
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_FAILED,
            attempt_number=0,
            payload={"status": "failure", "failure_kind": "provider_error"},
        ),
    ]

    state = _state(attempt_count=1, timeline_events=events)
    # Available workers: gemini, codex, openrouter
    available = frozenset({"gemini", "codex", "openrouter"})

    decision = _compute_legacy_route_decision(state, available)

    # Should NOT pick gemini
    assert decision.chosen_worker != "gemini"
    # Should pick codex or openrouter (depending on SUPPORTED_WORKER_TYPES order)
    assert decision.chosen_worker in {"codex", "openrouter"}


def test_legacy_rotation_falls_back_to_failed_if_all_exhausted():
    """Verify that rotation repeats a failed worker if NO untried workers are available."""
    # Attempt 0: Gemini failed
    # Attempt 1: Codex failed
    # Attempt 2: OpenRouter failed
    events = [
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_DISPATCHED,
            attempt_number=0,
            payload={"worker_type": "gemini"},
        ),
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_FAILED,
            attempt_number=0,
            payload={"status": "failure"},
        ),
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_DISPATCHED,
            attempt_number=1,
            payload={"worker_type": "codex"},
        ),
        TaskTimelineEventState(
            event_type=TimelineEventType.WORKER_FAILED,
            attempt_number=1,
            payload={"status": "failure"},
        ),
    ]

    state = _state(attempt_count=2, timeline_events=events)
    state.dispatch.worker_type = "codex"  # Last one tried

    # Available: only gemini and codex
    available = frozenset({"gemini", "codex"})

    decision = _compute_legacy_route_decision(state, available)

    # Both have failed, so it should just pick one that is NOT the current one (codex)
    assert decision.chosen_worker == "gemini"
