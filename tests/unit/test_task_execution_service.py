"""Unit tests for the task execution service."""

from __future__ import annotations

import asyncio

from sqlalchemy.pool import StaticPool

from db.base import Base
from orchestrator import (
    ApprovalCheckpoint,
    MemoryContext,
    OrchestratorState,
    RouteDecision,
    SessionRef,
    TaskRequest,
    WorkerDispatch,
    WorkerResult,
)
from orchestrator import execution as execution_module
from repositories import create_engine_from_url, create_session_factory
from workers import Worker, WorkerRequest


class _StaticWorker(Worker):
    """Minimal worker double used to initialize the service."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary=f"stubbed: {request.task_text}")


class _FakeGraph:
    """Graph double that records invocations and returns a valid final state."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ainvoke(self, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(payload)
        session = SessionRef.model_validate(payload["session"])
        task = TaskRequest.model_validate(payload["task"])
        return OrchestratorState(
            current_step="persist_memory",
            session=session,
            task=task,
            normalized_task_text=task.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="implementation_default",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(
                status="success",
                summary="fake graph completed",
            ),
            progress_updates=["task ingested", "worker result received"],
        ).model_dump(mode="json")


def test_task_execution_service_reuses_one_compiled_graph(
    monkeypatch,
) -> None:
    """The execution service should compile its graph once and reuse it across tasks."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    fake_graph = _FakeGraph()
    build_calls: list[Worker] = []

    def fake_build_orchestrator_graph(*, worker: Worker) -> _FakeGraph:
        build_calls.append(worker)
        return fake_graph

    monkeypatch.setattr(
        execution_module,
        "build_orchestrator_graph",
        fake_build_orchestrator_graph,
    )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )

    submission = execution_module.TaskSubmission(
        task_text="Run the task service",
        repo_url="https://github.com/natanayalo/code-agent",
    )

    _, persisted_one = service.create_task(submission)
    _, persisted_two = service.create_task(submission)

    asyncio.run(service._run_orchestrator(submission, persisted_one))
    asyncio.run(service._run_orchestrator(submission, persisted_two))

    assert len(build_calls) == 1
    assert len(fake_graph.calls) == 2
