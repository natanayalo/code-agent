from __future__ import annotations

from contextlib import contextmanager

import pytest

import orchestrator.temporal.activities as activities_module
from orchestrator.state import OrchestratorState
from orchestrator.temporal.activities import TaskExecutionActivities, _restore_task_trace_context


@pytest.mark.anyio
async def test_temporal_activity_restores_task_trace_context(monkeypatch) -> None:
    """Activity work must run under the trace context captured at task ingress."""
    seen_contexts: list[dict[str, str]] = []

    @contextmanager
    def record_context(trace_context: dict[str, str]):
        seen_contexts.append(trace_context)
        yield

    monkeypatch.setattr(
        "orchestrator.temporal.activities.with_restored_trace_context", record_context
    )

    class FakeService:
        async def _run_blocking(self, func, *args):
            return func(*args)

    class FakeActivity:
        service = FakeService()

        def _load_task_trace_context(self, task_id: str) -> dict[str, str]:
            assert task_id == "task-123"
            return {"traceparent": "00-abc-def-01"}

        @_restore_task_trace_context
        async def execute(self, task_id: str) -> str:
            assert task_id == "task-123"
            return "done"

    assert await FakeActivity().execute("task-123") == "done"
    assert seen_contexts == [{"traceparent": "00-abc-def-01"}]


@pytest.mark.anyio
async def test_temporal_activity_awaits_plain_async_nodes_directly() -> None:
    """Coroutine nodes must not be created inside the blocking worker thread."""

    class FakeService:
        async def _run_blocking(self, func, *args):
            raise AssertionError("plain async nodes must bypass _run_blocking")

    activity = object.__new__(TaskExecutionActivities)
    activity.service = FakeService()

    async def node(state: dict[str, object]) -> dict[str, object]:
        return {"seen": state["value"]}

    assert await activity._run_node(node, {"value": 42}) == {"seen": 42}


def test_temporal_activity_ignores_empty_node_updates() -> None:
    """Side-effect-only nodes may return no state update."""
    activity = object.__new__(TaskExecutionActivities)
    state: dict[str, object] = {"value": 42}

    activity._merge_updates(state, None)

    assert state == {"value": 42}


@pytest.mark.anyio
async def test_decompose_task_does_not_skip_generic_planning_event() -> None:
    """A plan event precedes decomposition and is not an idempotency marker."""

    class FakeService:
        async def _run_blocking(self, func, *args, **kwargs):
            return func(*args, **kwargs)

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-123", "task_text": "Inspect across files"},
            "timeline_events": [{"event_type": "task_planned", "message": "plan created"}],
        }
    )
    activity = object.__new__(TaskExecutionActivities)
    activity.service = FakeService()
    activity._get_current_state = lambda _task_id: state
    activity._persist_intermediate_state = lambda **_kwargs: None

    async def decompose_node(_state: dict[str, object]) -> dict[str, object]:
        return {
            "decomposed_plan": {
                "triggered": True,
                "status": "decomposed",
                "nodes": [
                    {
                        "node_id": "1",
                        "title": "Inspect",
                        "task_spec": {"goal": "Inspect"},
                        "node_kind": "inspect",
                        "aggregation_role": "context",
                    }
                ],
            }
        }

    activity.decompose_task_node = decompose_node

    result = await TaskExecutionActivities.decompose_task.__wrapped__(activity, "task-123")

    assert result["execution_shape"] == "decomposed"


@pytest.mark.anyio
async def test_classify_and_plan_does_not_skip_resolved_interaction(monkeypatch) -> None:
    """An operator response is not evidence that classification already ran."""

    class FakeService:
        async def _run_blocking(self, func, *args, **kwargs):
            return func(*args, **kwargs)

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-123", "task_text": "debug this"},
            "timeline_events": [
                {
                    "event_type": "interaction_resolved",
                    "message": "Clarification resolved.",
                }
            ],
        }
    )
    activity = object.__new__(TaskExecutionActivities)
    activity.service = FakeService()
    activity._get_current_state = lambda _task_id: state
    activity._persist_intermediate_state = lambda **_kwargs: None
    pipeline_calls: list[str] = []

    def node(name: str):
        async def execute(_state: dict[str, object]) -> dict[str, object]:
            pipeline_calls.append(name)
            return {}

        return execute

    monkeypatch.setattr(activities_module, "ingest_task", node("ingest"))
    monkeypatch.setattr(activities_module, "classify_task", node("classify"))
    monkeypatch.setattr(activities_module, "plan_task", node("plan"))
    monkeypatch.setattr(activities_module, "load_repo_profile_node", node("repo_profile"))
    monkeypatch.setattr(activities_module, "check_approval", node("approval"))
    activity.generate_task_spec_and_route_node = node("task_spec_and_route")

    async def ignore_progress(*_args, **_kwargs) -> None:
        return None

    activity._notify_progress = ignore_progress

    await TaskExecutionActivities.classify_and_plan.__wrapped__(activity, "task-123")

    assert pipeline_calls == [
        "ingest",
        "classify",
        "plan",
        "repo_profile",
        "task_spec_and_route",
        "approval",
    ]


@pytest.mark.anyio
async def test_classify_and_plan_skips_genuine_generation_marker(monkeypatch) -> None:
    """The real TaskSpec-and-route event remains the activity idempotency marker."""

    class FakeService:
        async def _run_blocking(self, func, *args, **kwargs):
            return func(*args, **kwargs)

    state = OrchestratorState.model_validate(
        {
            "task": {"task_id": "task-123", "task_text": "debug this"},
            "timeline_events": [
                {
                    "event_type": "task_spec_and_route_generated",
                    "message": "TaskSpec and route generated.",
                }
            ],
        }
    )
    activity = object.__new__(TaskExecutionActivities)
    activity.service = FakeService()
    activity._get_current_state = lambda _task_id: state

    async def fail_if_called(_state: dict[str, object]) -> dict[str, object]:
        raise AssertionError("classification pipeline should be skipped")

    monkeypatch.setattr(activities_module, "ingest_task", fail_if_called)

    result = await TaskExecutionActivities.classify_and_plan.__wrapped__(
        activity,
        "task-123",
    )

    assert result == {
        "requires_clarification": False,
        "requires_approval": False,
        "execution_task_queue": "task-execution-queue",
    }
