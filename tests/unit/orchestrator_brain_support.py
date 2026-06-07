"""Shared helpers for orchestrator brain unit tests."""

from __future__ import annotations

import asyncio

from orchestrator.state import OrchestratorState
from workers import Worker, WorkerRequest, WorkerResult


class _StaticWorker(Worker):
    """Worker test double returning a predefined result."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        self.requests.append(request)
        assert system_prompt is not None
        return self.result


class _ExplodingWorker(Worker):
    """Worker test double raising an exception."""

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        del request, system_prompt
        raise RuntimeError("planner crashed")


class _SlowWorker(Worker):
    """Worker test double sleeping long enough to trigger timeout."""

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        del request, system_prompt
        await asyncio.sleep(10.0)
        return WorkerResult(status="success", summary='{"suggested_worker":"codex"}')


def _state() -> OrchestratorState:
    return OrchestratorState.model_validate(
        {
            "task": {"task_text": "Route this task"},
            "task_kind": "implementation",
        }
    )
