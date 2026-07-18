"""Focused regression tests for run_queued_task heartbeat and load-guard paths."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TaskStatus, WorkerNodeStatus
from orchestrator import execution as execution_module
from orchestrator import execution_heartbeat_service, execution_runtime_service
from repositories import create_engine_from_url, create_session_factory
from workers import Worker, WorkerRequest, WorkerResult


class _StaticWorker(Worker):
    """Minimal worker double used only to initialize the execution service."""

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return WorkerResult(status="success", summary=f"stubbed: {request.task_text}")


class _RecordingProgressNotifier:
    """Capture progress events emitted by the execution service."""

    def __init__(self) -> None:
        self.events: list[execution_module.ProgressEvent] = []

    async def notify(
        self,
        *,
        submission: execution_module.TaskSubmission,
        event: execution_module.ProgressEvent,
    ) -> None:
        self.events.append(event)


def _make_task_service(
    *, notifier: _RecordingProgressNotifier | None = None
) -> execution_module.TaskExecutionService:
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    return execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
        progress_notifier=notifier,
    )


def _persisted_context(
    *, orchestration_runtime: str | None = "legacy"
) -> execution_module._PersistedTaskContext:
    return execution_module._PersistedTaskContext(
        user_id="user-1",
        session_id="session-1",
        channel="http",
        external_thread_id="thread-1",
        task_id="task-1",
        attempt_count=1,
        trace_context={},
        orchestration_runtime=orchestration_runtime,
    )


def _task_snapshot(
    *,
    status: str,
    last_error: str | None = None,
) -> execution_module.TaskSnapshot:
    timestamp = datetime.now(UTC)
    summary = execution_module.TaskSummarySnapshot(
        task_id="task-1",
        session_id="session-1",
        status=status,
        task_text="Run queued task",
        created_at=timestamp,
        updated_at=timestamp,
        last_error=last_error,
    )
    return execution_module.TaskSnapshot(**summary.model_dump())


@pytest.mark.anyio
async def test_heartbeat_loop_refreshes_worker_node_during_task(monkeypatch) -> None:
    """In-flight tasks should keep their worker registry heartbeat fresh."""
    service = _make_task_service()
    calls: list[str] = []
    worker_heartbeat_seen = asyncio.Event()

    async def fake_run_blocking(func, /, *args, **kwargs):
        name = getattr(func, "__name__", "")
        calls.append(name)
        if name == "_heartbeat_task_and_worker":
            worker_heartbeat_seen.set()
            return True, WorkerNodeStatus.ACTIVE
        raise AssertionError(f"unexpected heartbeat call: {name}")

    monkeypatch.setattr(
        execution_heartbeat_service,
        "_heartbeat_interval_seconds",
        lambda *, lease_seconds: 0.01,
    )
    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)

    heartbeat_task = asyncio.create_task(
        service._heartbeat_loop(task_id="task-1", worker_id="worker-a", lease_seconds=60)
    )
    await asyncio.wait_for(worker_heartbeat_seen.wait(), timeout=1)
    heartbeat_task.cancel()
    await asyncio.gather(heartbeat_task, return_exceptions=True)

    assert calls[:1] == ["_heartbeat_task_and_worker"]


@pytest.mark.anyio
async def test_heartbeat_loop_aborts_when_worker_node_heartbeat_fails(
    monkeypatch,
    caplog,
) -> None:
    """A missing worker node heartbeat should stop result processing for the run."""
    service = _make_task_service()

    async def fake_run_blocking(func, /, *args, **kwargs):
        name = getattr(func, "__name__", "")
        if name == "_heartbeat_task_and_worker":
            return True, None
        raise AssertionError(f"unexpected heartbeat call: {name}")

    monkeypatch.setattr(
        execution_heartbeat_service,
        "_heartbeat_interval_seconds",
        lambda *, lease_seconds: 0.01,
    )
    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)

    with caplog.at_level(logging.WARNING, logger="orchestrator.execution"):
        result = await asyncio.wait_for(
            service._heartbeat_loop(task_id="task-1", worker_id="worker-a", lease_seconds=60),
            timeout=1,
        )

    assert result is None
    assert "Heartbeat failed: worker node is not claimable" in caplog.text


@pytest.mark.anyio
async def test_run_queued_task_skips_missing_persisted_submission(caplog, monkeypatch) -> None:
    """Queue workers should bail out cleanly when the claimed task no longer reloads."""
    notifier = _RecordingProgressNotifier()
    service = _make_task_service(notifier=notifier)

    async def fake_run_blocking(func, /, *args, **kwargs):
        assert getattr(func, "__name__", "") == "_get_queued_task_ownership"
        return False, None

    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)

    with caplog.at_level(logging.WARNING, logger="orchestrator.execution"):
        await service.run_queued_task(task_id="missing-task", worker_id="worker-a")

    assert "Skipping queued task run: task no longer exists" in caplog.text
    assert notifier.events == []


@pytest.mark.anyio
async def test_run_queued_task_releases_nonlegacy_ownership_violation(monkeypatch, caplog) -> None:
    """A directly invoked legacy worker must fail closed for a Temporal task."""
    service = _make_task_service()
    released: list[tuple[str, str]] = []

    async def fake_run_blocking(func, /, *args, **kwargs):
        if getattr(func, "__name__", "") == "_get_queued_task_ownership":
            return True, "temporal"
        if getattr(func, "__name__", "") == "_load_submission_for_task":
            raise AssertionError("non-legacy ownership must be checked before submission loading")
        if getattr(func, "__name__", "") == "_release_legacy_ownership_violation":
            released.append((kwargs["task_id"], kwargs["worker_id"]))
            return None
        raise AssertionError(f"unexpected blocking call: {getattr(func, '__name__', '')}")

    async def fail_run_orchestrator(*_args, **_kwargs):
        raise AssertionError("A non-legacy task must not execute on the legacy worker")

    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fail_run_orchestrator)

    with caplog.at_level(logging.ERROR, logger="orchestrator.execution"):
        await service.run_queued_task(task_id="task-1", worker_id="worker-a")

    assert released == [("task-1", "worker-a")]
    assert "Legacy worker refused task with non-legacy runtime ownership" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("runtime", ["temporal", None])
async def test_invalid_nonlegacy_submission_never_reaches_legacy_validation_handler(
    monkeypatch, runtime
) -> None:
    """Invalid Temporal or unknown payloads must be rejected before validation."""
    service = _make_task_service()
    calls: list[str] = []

    try:
        execution_module.TaskSubmission.model_validate({"task_text": None})
    except ValidationError as exc:
        invalid_submission = exc
    else:  # pragma: no cover - guards the test fixture's invalid input
        raise AssertionError("Expected an invalid TaskSubmission fixture")

    async def fake_run_blocking(func, /, *args, **kwargs):
        name = getattr(func, "__name__", "")
        calls.append(name)
        if name == "_get_queued_task_ownership":
            return True, runtime
        if name == "_load_submission_for_task":
            raise invalid_submission
        if name == "_release_legacy_ownership_violation":
            return None
        if name in {"_record_task_attempt_error", "_release_task_terminal_failure"}:
            raise AssertionError("non-legacy tasks must not enter validation handling")
        raise AssertionError(f"unexpected blocking call: {name}")

    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)

    await service.run_queued_task(task_id="task-1", worker_id="worker-a")

    assert calls == ["_get_queued_task_ownership", "_release_legacy_ownership_violation"]


@pytest.mark.anyio
async def test_invalid_legacy_submission_reaches_legacy_validation_handler(monkeypatch) -> None:
    """Only an owned legacy task should be classified as an invalid submission."""
    service = _make_task_service()
    calls: list[str] = []

    try:
        execution_module.TaskSubmission.model_validate({"task_text": None})
    except ValidationError as exc:
        invalid_submission = exc
    else:  # pragma: no cover - guards the test fixture's invalid input
        raise AssertionError("Expected an invalid TaskSubmission fixture")

    async def fake_run_blocking(func, /, *args, **kwargs):
        name = getattr(func, "__name__", "")
        calls.append(name)
        if name == "_get_queued_task_ownership":
            return True, "legacy"
        if name == "_load_submission_for_task":
            raise invalid_submission
        if name in {"_record_task_attempt_error", "_release_task_terminal_failure"}:
            return None
        raise AssertionError(f"unexpected blocking call: {name}")

    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)

    await service.run_queued_task(task_id="task-1", worker_id="worker-a")

    assert calls == [
        "_get_queued_task_ownership",
        "_load_submission_for_task",
        "_record_task_attempt_error",
        "_release_task_terminal_failure",
    ]


@pytest.mark.anyio
async def test_run_queued_task_aborts_cleanly_when_task_was_cancelled(monkeypatch) -> None:
    """If heartbeat ends first and the task was cancelled, execution should stop without requeue."""
    notifier = _RecordingProgressNotifier()
    service = _make_task_service(notifier=notifier)
    submission = execution_module.TaskSubmission(task_text="Cancelled task")
    persisted = _persisted_context()
    release_calls: list[str] = []

    async def fake_run_blocking(func, /, *args, **kwargs):
        name = getattr(func, "__name__", "")
        if name == "_get_queued_task_ownership":
            return True, "legacy"
        if name == "_load_submission_for_task":
            return submission, persisted
        if name == "get_task":
            return _task_snapshot(
                status=TaskStatus.FAILED.value,
                last_error="Task cancelled by operator.",
            )
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)
    monkeypatch.setattr(
        service, "_release_task_failure", lambda **kwargs: release_calls.append("failure")
    )
    monkeypatch.setattr(
        service, "_release_task_terminal_failure", lambda **kwargs: release_calls.append("terminal")
    )

    await service.run_queued_task(task_id="task-1", worker_id="worker-a")

    assert [event.phase for event in notifier.events] == ["started", "running"]
    assert release_calls == []


@pytest.mark.anyio
async def test_run_queued_task_aborts_cleanly_when_lease_is_lost(monkeypatch, caplog) -> None:
    """Stop execution cleanly when heartbeat ends before cancellation is requested."""
    notifier = _RecordingProgressNotifier()
    service = _make_task_service(notifier=notifier)
    submission = execution_module.TaskSubmission(task_text="Lease lost task")
    persisted = _persisted_context()
    release_calls: list[str] = []

    async def fake_run_blocking(func, /, *args, **kwargs):
        name = getattr(func, "__name__", "")
        if name == "_get_queued_task_ownership":
            return True, "legacy"
        if name == "_load_submission_for_task":
            return submission, persisted
        if name == "get_task":
            return _task_snapshot(status=TaskStatus.IN_PROGRESS.value, last_error=None)
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)
    monkeypatch.setattr(
        service, "_release_task_failure", lambda **kwargs: release_calls.append("failure")
    )
    monkeypatch.setattr(
        service, "_release_task_terminal_failure", lambda **kwargs: release_calls.append("terminal")
    )

    with caplog.at_level(logging.WARNING, logger="orchestrator.execution"):
        await service.run_queued_task(task_id="task-1", worker_id="worker-a")

    assert "Task execution aborted: lease lost or stolen" in caplog.text
    assert [event.phase for event in notifier.events] == ["started", "running"]
    assert release_calls == []


@pytest.mark.anyio
async def test_run_queued_task_aborts_when_orchestrator_completes_after_cancellation_request(
    monkeypatch, caplog
) -> None:
    """If cancellation loses the race, the heartbeat failure should still win over the result."""
    notifier = _RecordingProgressNotifier()
    service = _make_task_service(notifier=notifier)
    submission = execution_module.TaskSubmission(task_text="Late cancel task")
    persisted = _persisted_context()
    persist_calls: list[str] = []

    async def fake_run_blocking(func, /, *args, **kwargs):
        name = getattr(func, "__name__", "")
        if name == "_get_queued_task_ownership":
            return True, "legacy"
        if name == "_load_submission_for_task":
            return submission, persisted
        if name == "get_task":
            raise AssertionError("task snapshot should not reload on heartbeat abort")
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> object:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            return object()

    async def fake_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
        return None

    monkeypatch.setattr(service, "_run_blocking", fake_run_blocking)
    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", fake_heartbeat_loop)
    monkeypatch.setattr(
        service,
        "_persist_execution_outcome",
        lambda **kwargs: persist_calls.append("persisted"),
    )

    with caplog.at_level(logging.WARNING, logger="orchestrator.execution"):
        await service.run_queued_task(task_id="task-1", worker_id="worker-a")

    assert (
        "Orchestrator task completed despite cancellation request. "
        "Aborting due to heartbeat failure." in caplog.text
    )
    assert [event.phase for event in notifier.events] == ["started", "running"]
    assert persist_calls == []


@pytest.mark.anyio
async def test_wait_aborts_when_heartbeat_and_orchestrator_finish_together(caplog) -> None:
    """Heartbeat failure should win even when the orchestrator result is also ready."""
    service = _make_task_service()

    async def complete_orchestrator() -> object:
        return object()

    async def complete_heartbeat() -> None:
        return None

    orchestrator_task = asyncio.create_task(complete_orchestrator())
    heartbeat_task = asyncio.create_task(complete_heartbeat())
    await asyncio.gather(orchestrator_task, heartbeat_task)

    with caplog.at_level(logging.WARNING, logger="orchestrator.execution"):
        result = await execution_runtime_service._wait_for_orchestrator_or_heartbeat(
            service,
            "task-1",
            orchestrator_task,
            heartbeat_task,
        )

    assert result is None
    assert "Orchestrator task completed while heartbeat failed" in caplog.text
