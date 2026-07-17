"""Unit coverage for the Temporal node-wave execution activity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import orchestrator.temporal.activities as activities_module
from orchestrator.node_execution import NodeActivityRequest, NodeActivityResultRef
from orchestrator.state import OrchestratorState
from orchestrator.temporal.activities import TaskExecutionActivities


def _state(*, with_dependency: bool = False) -> OrchestratorState:
    node = {
        "node_id": "node",
        "title": "Node",
        "task_spec": {"goal": "Run node"},
        "node_kind": "implement",
    }
    if with_dependency:
        node["depends_on"] = ["missing"]
    return OrchestratorState.model_validate(
        {
            "task": {"task_id": "task", "task_text": "Run task"},
            "route": {"chosen_worker": "codex"},
            "decomposed_plan": {"status": "decomposed", "nodes": [node]},
        }
    )


def _activity(state: OrchestratorState) -> TaskExecutionActivities:
    instance = object.__new__(TaskExecutionActivities)

    async def _run_blocking(func, *args, **kwargs):
        return func(*args, **kwargs)

    instance.service = SimpleNamespace(
        _run_blocking=_run_blocking,
        session_factory=object(),
        worker=SimpleNamespace(),
    )
    instance._get_current_state = lambda _task_id: state
    instance._load_task_trace_context = lambda _task_id: {}
    return instance


@pytest.mark.anyio
async def test_run_decomposed_node_reconstructs_request_and_returns_compact_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    activity = _activity(state)
    digest = "a" * 64
    request = SimpleNamespace(session_id=None)
    captured: dict[str, object] = {}

    class FakeNodeExecutionService:
        def __init__(self, _session_factory) -> None:
            pass

        async def execute(self, **kwargs):
            captured.update(kwargs)
            return (
                NodeActivityResultRef(
                    node_id="node",
                    logical_activity_key="node-activity:v1:plan:node:1",
                    status="completed",
                    result_digest="b" * 64,
                    continuation="continue",
                ),
                None,
            )

    monkeypatch.setattr(activities_module, "NodeExecutionService", FakeNodeExecutionService)
    monkeypatch.setattr(activities_module, "_build_worker_request", lambda *args, **kwargs: request)
    monkeypatch.setattr(activities_module, "_effective_input_evidence", lambda *args: ({}, digest))
    monkeypatch.setattr(activities_module.activity, "heartbeat", lambda: None)

    result = await activity.run_decomposed_node(
        "task",
        NodeActivityRequest(
            task_id="task",
            plan_id="plan",
            node_id="node",
            logical_attempt=1,
            logical_activity_key="node-activity:v1:plan:node:1",
            effective_input_digest=digest,
        ).model_dump(mode="json"),
    )

    assert result["status"] == "completed"
    assert captured["effective_input_summary"] == {}


@pytest.mark.anyio
async def test_run_decomposed_node_rejects_missing_dependency_outcome() -> None:
    state = _state(with_dependency=True)
    activity = _activity(state)

    with pytest.raises(ValueError, match="Dependency missing outcome"):
        await activity.run_decomposed_node(
            "task",
            NodeActivityRequest(
                task_id="task",
                plan_id="plan",
                node_id="node",
                logical_attempt=1,
                logical_activity_key="node-activity:v1:plan:node:1",
                effective_input_digest="a" * 64,
            ).model_dump(mode="json"),
        )


@pytest.mark.anyio
async def test_run_decomposed_node_cancels_worker_when_temporal_heartbeat_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()
    activity = _activity(state)
    digest = "a" * 64
    cancelled = asyncio.Event()

    class FakeNodeExecutionService:
        def __init__(self, _session_factory) -> None:
            pass

        async def execute(self, **_kwargs):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(activities_module, "NodeExecutionService", FakeNodeExecutionService)
    monkeypatch.setattr(
        activities_module,
        "_build_worker_request",
        lambda *args, **kwargs: SimpleNamespace(session_id=None),
    )
    monkeypatch.setattr(activities_module, "_effective_input_evidence", lambda *args: ({}, digest))
    monkeypatch.setattr(
        activities_module.activity,
        "heartbeat",
        lambda: (_ for _ in ()).throw(RuntimeError("heartbeat failed")),
    )

    with pytest.raises(RuntimeError, match="heartbeat failed"):
        await activity.run_decomposed_node(
            "task",
            NodeActivityRequest(
                task_id="task",
                plan_id="plan",
                node_id="node",
                logical_attempt=1,
                logical_activity_key="node-activity:v1:plan:node:1",
                effective_input_digest=digest,
            ).model_dump(mode="json"),
        )

    assert cancelled.is_set()
