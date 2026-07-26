"""Heartbeat coverage for long-running Temporal verification activities."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.state import OrchestratorState
from orchestrator.temporal import activities as activities_module
from orchestrator.temporal.activities import TaskExecutionActivities


@pytest.mark.anyio
async def test_verify_result_heartbeats_while_verification_runs(monkeypatch) -> None:
    """A worker restart can recover a verification Activity without its long timeout."""
    state = OrchestratorState(task={"task_id": "task-id", "task_text": "Verify this task"})
    activities = object.__new__(TaskExecutionActivities)
    activities.service = SimpleNamespace(
        _run_blocking=lambda func, *args, **kwargs: _run_blocking(func, *args, **kwargs)
    )
    activities._get_current_state = lambda _task_id: state
    activities._has_event = lambda *_args: False
    activities._persist_intermediate_state = lambda **_kwargs: None
    activities.verify_result_node = object()
    activities.review_result_node = object()
    activities._merge_updates = lambda _state, _updates: None

    verification_started = asyncio.Event()
    release_verification = asyncio.Event()

    async def run_node(_node, _state):
        verification_started.set()
        await release_verification.wait()
        return {}

    activities._run_node = run_node
    heartbeats: list[None] = []
    monkeypatch.setattr(activities_module.activity, "heartbeat", lambda: heartbeats.append(None))
    original_sleep = asyncio.sleep
    repeated_heartbeats = asyncio.Event()

    async def advance_heartbeat_interval(delay: float) -> None:
        if delay == 5 and len(heartbeats) >= 2:
            repeated_heartbeats.set()
        await original_sleep(0)

    monkeypatch.setattr(activities_module.asyncio, "sleep", advance_heartbeat_interval)

    verification_task = asyncio.create_task(
        TaskExecutionActivities.verify_result.__wrapped__(activities, "task-id")
    )
    await verification_started.wait()
    await asyncio.wait_for(repeated_heartbeats.wait(), timeout=1)
    release_verification.set()
    await verification_task

    assert len(heartbeats) >= 2


async def _run_blocking(func, *args, **kwargs):
    return func(*args, **kwargs)
