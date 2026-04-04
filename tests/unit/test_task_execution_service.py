"""Unit tests for the task execution service."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TaskStatus
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
from repositories import (
    SessionRepository,
    TaskRepository,
    UserRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
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


@pytest.mark.anyio
async def test_submit_task_moves_sync_persistence_work_off_thread(monkeypatch) -> None:
    """Async task execution should route sync persistence work through anyio's threadpool."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    fake_graph = _FakeGraph()
    monkeypatch.setattr(
        execution_module,
        "build_orchestrator_graph",
        lambda *, worker: fake_graph,
    )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Run the task service",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="http",
        external_thread_id="thread-1",
        task_id="task-1",
    )

    snapshot = execution_module.TaskSnapshot(
        task_id="task-1",
        session_id="session-1",
        status="completed",
        task_text=submission.task_text,
        repo_url=submission.repo_url,
        branch=submission.branch,
        priority=submission.priority,
        chosen_worker="codex",
        route_reason="implementation_default",
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    recorded_calls: list[str] = []

    async def fake_run_sync(func):
        recorded_calls.append(func.func.__name__)
        return func()

    def fake_mark_task_in_progress(*, task_id: str) -> None:
        return None

    def fake_persist_execution_outcome(**kwargs) -> None:
        return None

    def fake_get_task(task_id: str) -> execution_module.TaskSnapshot:
        return snapshot

    def fake_log_task_outcome(task_snapshot: execution_module.TaskSnapshot) -> None:
        return None

    monkeypatch.setattr(execution_module.to_thread, "run_sync", fake_run_sync)
    monkeypatch.setattr(service, "_mark_task_in_progress", fake_mark_task_in_progress)
    monkeypatch.setattr(service, "_persist_execution_outcome", fake_persist_execution_outcome)
    monkeypatch.setattr(service, "get_task", fake_get_task)
    monkeypatch.setattr(service, "_log_task_outcome", fake_log_task_outcome)

    await service.submit_task(submission, persisted)

    assert recorded_calls == [
        "fake_mark_task_in_progress",
        "fake_persist_execution_outcome",
        "fake_get_task",
    ]


@pytest.mark.anyio
async def test_submit_task_marks_task_failed_when_outcome_persistence_crashes(
    monkeypatch,
) -> None:
    """Persistence failures should not leave the task stuck in progress."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Fail after orchestration finishes",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        return OrchestratorState(
            current_step="persist_memory",
            session=SessionRef(
                session_id=persisted.session_id,
                user_id=persisted.user_id,
                channel=persisted.channel,
                external_thread_id=persisted.external_thread_id,
                active_task_id=persisted.task_id,
                status="active",
            ),
            task=TaskRequest(
                task_id=persisted.task_id,
                task_text=submission.task_text,
                repo_url=submission.repo_url,
                branch=submission.branch,
                priority=submission.priority,
                worker_override=submission.worker_override,
                constraints=dict(submission.constraints),
                budget=dict(submission.budget),
            ),
            normalized_task_text=submission.task_text,
            task_kind="implementation",
            memory=MemoryContext(),
            route=RouteDecision(
                chosen_worker="codex",
                route_reason="implementation_default",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(status="success", summary="orchestrator finished"),
        )

    def fake_persist_execution_outcome(**kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_persist_execution_outcome", fake_persist_execution_outcome)

    await service.submit_task(submission, persisted)

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.status == TaskStatus.FAILED.value
    assert task_snapshot.latest_run is None


def test_create_task_recovers_from_duplicate_user_and_session_race(monkeypatch) -> None:
    """Task creation should recover if another request inserts the user/session first."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        existing_user = user_repo.create(
            external_user_id="http:test-user",
            display_name="Existing User",
        )
        existing_session = session_repo.create(
            user_id=existing_user.id,
            channel="http",
            external_thread_id="thread-race",
        )

    original_get_user = UserRepository.get_by_external_user_id
    original_get_session = SessionRepository.get_by_channel_thread
    user_calls = 0
    session_calls = 0

    def stale_get_user(self, external_user_id: str):
        nonlocal user_calls
        user_calls += 1
        if user_calls == 1:
            return None
        return original_get_user(self, external_user_id)

    def stale_get_session(self, *, channel: str, external_thread_id: str):
        nonlocal session_calls
        session_calls += 1
        if session_calls == 1:
            return None
        return original_get_session(
            self,
            channel=channel,
            external_thread_id=external_thread_id,
        )

    monkeypatch.setattr(UserRepository, "get_by_external_user_id", stale_get_user)
    monkeypatch.setattr(SessionRepository, "get_by_channel_thread", stale_get_session)

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    task_snapshot, persisted = service.create_task(
        execution_module.TaskSubmission(
            task_text="Recover from create race",
            repo_url="https://github.com/natanayalo/code-agent",
            session=execution_module.SubmissionSession(
                external_user_id="http:test-user",
                external_thread_id="thread-race",
            ),
        )
    )

    assert persisted.user_id == existing_user.id
    assert persisted.session_id == existing_session.id
    assert task_snapshot.status == TaskStatus.PENDING.value

    with session_scope(session_factory) as session:
        user_repo = UserRepository(session)
        session_repo = SessionRepository(session)
        task_repo = TaskRepository(session)

        assert user_repo.get_by_external_user_id("http:test-user") is not None
        recovered_session = session_repo.get_by_channel_thread(
            channel="http",
            external_thread_id="thread-race",
        )
        assert recovered_session is not None
        assert len(session_repo.list_by_user(existing_user.id)) == 1
        assert len(task_repo.list_by_session(existing_session.id)) == 1
