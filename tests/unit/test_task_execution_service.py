"""Unit tests for the task execution service."""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TaskStatus, WorkerRunStatus
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
    InboundDeliveryRepository,
    SessionRepository,
    SessionStateRepository,
    TaskRepository,
    UserRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import ArtifactReference, Worker, WorkerRequest


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
                route_reason="cheap_mechanical_change",
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


class _RecordingProgressNotifier:
    """Capture progress events emitted by the task execution service."""

    def __init__(self) -> None:
        self.events: list[execution_module.ProgressEvent] = []

    async def notify(
        self,
        *,
        submission: execution_module.TaskSubmission,
        event: execution_module.ProgressEvent,
    ) -> None:
        self.events.append(event)


def test_validate_callback_url_accepts_hostname_with_public_resolution(monkeypatch) -> None:
    """Hostnames that resolve only to public IPs should still be allowed."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        assert host == "callbacks.example.com"
        assert port == 443
        assert type == socket.SOCK_STREAM
        assert proto == socket.IPPROTO_TCP
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
        ]

    monkeypatch.setattr(execution_module.socket, "getaddrinfo", fake_getaddrinfo)

    assert (
        execution_module._validate_callback_url("https://callbacks.example.com/status")
        == "https://callbacks.example.com/status"
    )


def test_validate_callback_url_rejects_hostname_with_private_resolution(monkeypatch) -> None:
    """Hostname callbacks should be rejected when DNS resolves to a private address."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        assert host == "callbacks.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", port))]

    monkeypatch.setattr(execution_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private or local address"):
        execution_module._validate_callback_url("https://callbacks.example.com/status")


def test_validate_callback_url_rejects_hostname_with_mixed_public_and_private_resolution(
    monkeypatch,
) -> None:
    """Mixed DNS answers should fail closed when any resolved address is unsafe."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        assert host == "callbacks.example.com"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(execution_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private or local address"):
        execution_module._validate_callback_url("https://callbacks.example.com/status")


def test_validate_callback_url_rejects_unresolvable_hostname(monkeypatch) -> None:
    """Unresolvable callback hosts should fail closed."""

    def fake_getaddrinfo(host: str, port: int, *, type: int, proto: int):
        raise socket.gaierror("boom")

    monkeypatch.setattr(execution_module.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="could not be resolved"):
        execution_module._validate_callback_url("https://callbacks.example.com/status")


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

    def fake_build_orchestrator_graph(*, worker: Worker, gemini_worker=None) -> _FakeGraph:
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


def test_workspace_id_from_artifacts_supports_url_and_custom_workspace_uris() -> None:
    """Workspace ids should still be inferred when artifact URIs are not plain local paths."""
    assert (
        execution_module._workspace_id_from_artifacts(
            [
                ArtifactReference(
                    name="workspace",
                    uri="https://artifacts.example.com/runs/workspace-1234?signature=abc",
                    artifact_type="workspace",
                )
            ]
        )
        == "workspace-1234"
    )
    assert (
        execution_module._workspace_id_from_artifacts(
            [
                ArtifactReference(
                    name="workspace",
                    uri="workspace://workspace-5678",
                    artifact_type="workspace",
                )
            ]
        )
        == "workspace-5678"
    )


def test_create_task_outcome_returns_existing_task_for_duplicate_delivery() -> None:
    """Duplicate delivery keys should resolve to the original task without new persistence."""
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
        task_text="Run the task service",
        session=execution_module.SubmissionSession(
            channel="telegram",
            external_user_id="telegram:user:42",
            external_thread_id="telegram:chat:100",
        ),
    )
    delivery_key = execution_module.DeliveryKey(channel="telegram", delivery_id="123")

    first = service.create_task_outcome(submission, delivery_key=delivery_key)
    second = service.create_task_outcome(submission, delivery_key=delivery_key)

    assert first.duplicate is False
    assert first.persisted is not None
    assert second.duplicate is True
    assert second.persisted is None
    assert second.task_snapshot.task_id == first.task_snapshot.task_id

    with session_scope(session_factory) as session:
        tasks = TaskRepository(session).list_by_session(first.task_snapshot.session_id)
        assert len(tasks) == 1


def test_create_task_outcome_recovers_stale_delivery_without_task_id() -> None:
    """A stale delivery claim without a linked task should be recoverable on retry."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_scope(session_factory) as session:
        InboundDeliveryRepository(session).create(
            channel="telegram",
            delivery_id="stale-123",
            task_id=None,
        )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    submission = execution_module.TaskSubmission(
        task_text="Recover stale delivery",
        session=execution_module.SubmissionSession(
            channel="telegram",
            external_user_id="telegram:user:42",
            external_thread_id="telegram:chat:100",
        ),
    )

    outcome = service.create_task_outcome(
        submission,
        delivery_key=execution_module.DeliveryKey(channel="telegram", delivery_id="stale-123"),
    )

    assert outcome.duplicate is False
    assert outcome.persisted is not None

    with session_scope(session_factory) as session:
        delivery = InboundDeliveryRepository(session).get_by_channel_delivery(
            channel="telegram",
            delivery_id="stale-123",
        )
        assert delivery is not None
        assert delivery.task_id == outcome.task_snapshot.task_id


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
        lambda *, worker, gemini_worker=None: fake_graph,
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
        route_reason="cheap_mechanical_change",
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
async def test_submit_task_emits_progress_notifications_for_success(monkeypatch) -> None:
    """Successful task execution should emit started, running, and completed updates."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    notifier = _RecordingProgressNotifier()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )
    submission = execution_module.TaskSubmission(task_text="Notify success")
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="telegram",
        external_thread_id="telegram:chat:100",
        task_id="task-1",
    )

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
                route_reason="cheap_mechanical_change",
                override_applied=False,
            ),
            approval=ApprovalCheckpoint(),
            dispatch=WorkerDispatch(worker_type="codex"),
            result=WorkerResult(status="success", summary="all done"),
        )

    completed_snapshot = execution_module.TaskSnapshot(
        task_id=persisted.task_id,
        session_id=persisted.session_id,
        status="completed",
        task_text=submission.task_text,
        priority=submission.priority,
        chosen_worker="codex",
        route_reason="cheap_mechanical_change",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        latest_run=execution_module.WorkerRunSnapshot(
            run_id="run-1",
            worker_type="codex",
            status="success",
            started_at=datetime.now(),
            summary="all done",
        ),
    )

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_mark_task_in_progress", lambda *, task_id: None)
    monkeypatch.setattr(service, "_persist_execution_outcome", lambda **kwargs: None)
    monkeypatch.setattr(service, "get_task", lambda task_id: completed_snapshot)
    monkeypatch.setattr(service, "_log_task_outcome", lambda task_snapshot: None)

    await service.submit_task(submission, persisted)

    assert [event.phase for event in notifier.events] == ["started", "running", "completed"]
    assert notifier.events[-1].summary == "all done"


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
                route_reason="cheap_mechanical_change",
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


@pytest.mark.anyio
async def test_submit_task_logs_and_exits_when_failed_task_cannot_be_reloaded(
    monkeypatch,
    caplog,
) -> None:
    """The background task should not crash if the failed task snapshot cannot be reloaded."""
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
        task_text="Fail and skip reload",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="http",
        external_thread_id="thread-1",
        task_id="task-1",
    )

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        raise RuntimeError("orchestrator boom")

    def fake_mark_task_in_progress(*, task_id: str) -> None:
        return None

    def fake_mark_task_failed(*, task_id: str) -> None:
        return None

    def fake_get_task(task_id: str) -> None:
        return None

    def fake_log_task_outcome(task_snapshot: execution_module.TaskSnapshot) -> None:
        raise AssertionError("should not log a missing snapshot")

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_mark_task_in_progress", fake_mark_task_in_progress)
    monkeypatch.setattr(service, "_mark_task_failed", fake_mark_task_failed)
    monkeypatch.setattr(service, "get_task", fake_get_task)
    monkeypatch.setattr(service, "_log_task_outcome", fake_log_task_outcome)

    with caplog.at_level(logging.ERROR):
        await service.submit_task(submission, persisted)

    assert "Failed to reload task snapshot after marking a background task as failed" in caplog.text


@pytest.mark.anyio
async def test_submit_task_emits_failed_notification_when_snapshot_reload_fails(
    monkeypatch,
) -> None:
    """Failure notifications should still be emitted when the final task snapshot is missing."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    notifier = _RecordingProgressNotifier()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )
    submission = execution_module.TaskSubmission(task_text="Notify failure")
    persisted = execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="http",
        external_thread_id="thread-1",
        task_id="task-1",
    )

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_mark_task_in_progress", lambda *, task_id: None)
    monkeypatch.setattr(service, "_mark_task_failed", lambda *, task_id: None)
    monkeypatch.setattr(service, "get_task", lambda task_id: None)
    monkeypatch.setattr(service, "_log_task_outcome", lambda task_snapshot: None)

    await service.submit_task(submission, persisted)

    assert [event.phase for event in notifier.events] == ["started", "running", "failed"]
    assert notifier.events[-1].summary == (
        "Task execution failed and the final snapshot could not be reloaded."
    )


def test_persist_execution_outcome_creates_error_worker_run_without_result() -> None:
    """Missing worker results should still leave an error worker-run record for observability."""
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
        task_text="Persist an error run",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = OrchestratorState(
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
            route_reason="cheap_mechanical_change",
            override_applied=False,
        ),
        approval=ApprovalCheckpoint(),
        dispatch=WorkerDispatch(worker_type="codex"),
        result=None,
    )

    started_at = datetime.now()
    finished_at = datetime.now()
    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.status == TaskStatus.FAILED.value
    assert task_snapshot.chosen_worker == "codex"
    assert task_snapshot.route_reason == "cheap_mechanical_change"
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.session_id == persisted.session_id
    assert task_snapshot.latest_run.status == WorkerRunStatus.ERROR.value
    assert task_snapshot.latest_run.summary == "Worker did not return a result."
    assert task_snapshot.latest_run.verifier_outcome is None
    assert task_snapshot.latest_run.artifact_index == []
    assert task_snapshot.latest_run.files_changed_count == 0


def test_persist_execution_outcome_persists_session_state_update() -> None:
    """Execution persistence should store the compact session working state."""
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
        task_text="Persist session state",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = OrchestratorState(
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
            route_reason="cheap_mechanical_change",
            override_applied=False,
        ),
        approval=ApprovalCheckpoint(),
        dispatch=WorkerDispatch(worker_type="codex"),
        result=WorkerResult(
            status="success",
            summary="done",
            requested_permission="workspace_write",
            budget_usage={"iterations_used": 1, "tool_calls_used": 1},
            files_changed=["orchestrator/execution.py"],
        ),
        verification={
            "status": "passed",
            "summary": "Verifier accepted the run.",
            "items": [{"label": "worker_status", "status": "passed"}],
        },
        session_state_update={
            "active_goal": "Persist session state",
            "decisions_made": {"worker": "codex"},
            "identified_risks": {"network": "restricted"},
            "files_touched": ["orchestrator/execution.py"],
        },
    )

    started_at = datetime.now()
    finished_at = datetime.now()
    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.requested_permission == "workspace_write"
    assert task_snapshot.latest_run.budget_usage == {
        "iterations_used": 1,
        "tool_calls_used": 1,
    }
    assert task_snapshot.latest_run.verifier_outcome == {
        "status": "passed",
        "summary": "Verifier accepted the run.",
        "items": [{"label": "worker_status", "status": "passed", "message": None}],
    }

    with session_scope(session_factory) as session:
        session_state_repo = SessionStateRepository(session)
        session_state = session_state_repo.get(persisted.session_id)

        assert session_state is not None
        assert session_state.active_goal == "Persist session state"
        assert session_state.decisions_made == {"worker": "codex"}
        assert session_state.identified_risks == {"network": "restricted"}
        assert session_state.files_touched == ["orchestrator/execution.py"]


def test_persist_execution_outcome_accepts_raw_verification_mapping() -> None:
    """Execution persistence should tolerate verification payloads that are plain dicts."""
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
        task_text="Persist raw verification mapping",
        repo_url="https://github.com/natanayalo/code-agent",
    )
    _, persisted = service.create_task(submission)

    state = OrchestratorState.model_construct(
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
            route_reason="cheap_mechanical_change",
            override_applied=False,
        ),
        approval=ApprovalCheckpoint(),
        dispatch=WorkerDispatch(worker_type="codex"),
        result=WorkerResult(status="success", summary="done"),
        verification={
            "status": "passed",
            "summary": "Verifier accepted the run.",
            "items": [],
        },
    )

    service._persist_execution_outcome(
        task_id=persisted.task_id,
        state=state,
        started_at=datetime.now(),
        finished_at=datetime.now(),
    )

    task_snapshot = service.get_task(persisted.task_id)
    assert task_snapshot is not None
    assert task_snapshot.latest_run is not None
    assert task_snapshot.latest_run.verifier_outcome == {
        "status": "passed",
        "summary": "Verifier accepted the run.",
        "items": [],
    }


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
