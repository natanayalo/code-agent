# ruff: noqa: F403, F405
"""Behavior-focused task execution service tests."""

from __future__ import annotations

from tests.unit.task_execution_service_support import *  # noqa: F403


def _make_orchestrator_state_from_persisted(submission, persisted, result=None):
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
        result=result,
    )


@pytest.mark.anyio
async def test_submit_task_moves_sync_persistence_work_off_thread(monkeypatch) -> None:
    """Async task execution should route sync persistence work through anyio's threadpool."""
    _, session_factory = _make_task_service()

    fake_graph = _FakeGraph()
    monkeypatch.setattr(
        execution_module,
        "build_orchestrator_graph",
        lambda *, worker, **kwargs: fake_graph,
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
        attempt_count=0,
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

    def fake_persist_execution_outcome(**kwargs):
        return SimpleNamespace(
            task_id="task-1",
            session_id="session-1",
            task_constraints=None,
            worker_run_id="run-1",
        )

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
        "_load_submission_for_task",
        "fake_mark_task_in_progress",
        "_get_count",
        "fake_persist_execution_outcome",
        "fake_get_task",
    ]


def test_create_task_clamps_scout_budget_and_forces_read_only() -> None:
    """Scout task constraints should be clamped to bounds and forced read-only."""
    service, session_factory = _make_task_service()

    submission = execution_module.TaskSubmission(
        task_text="Run a scout task",
        repo_url="https://github.com/natanayalo/code-agent",
        constraints={"task_type": "scout", "read_only": False},
        budget={
            "max_iterations": 10,
            "worker_timeout_seconds": 600,
            "max_tool_calls": 50,
            "max_shell_commands": 50,
            "max_retries": 5,
        },
    )

    snapshot, persisted = service.create_task(submission)

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(persisted.task_id)
        assert task is not None
        assert task.queue_lane == "scout"

        constraints = dict(task.constraints)
        budget = dict(task.budget)

        # Constraints are forced to read_only=True
        assert constraints.get("read_only") is True

        # Budget clamped to scout caps
        assert budget.get("max_iterations") == 3
        assert budget.get("worker_timeout_seconds") == 180
        assert budget.get("max_tool_calls") == 8
        assert budget.get("max_shell_commands") == 8
        assert budget.get("max_retries") == 0
        assert budget.get("execution_mode") == "unattended"


def test_create_task_persists_repair_for_task_id() -> None:
    """Repair tasks should keep their source-task link in DB and snapshots."""
    service, session_factory = _make_task_service()
    source_snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="Original task",
            repo_url="https://github.com/natanayalo/code-agent",
        )
    )
    repair_snapshot, persisted = service.create_task(
        execution_module.TaskSubmission(
            task_text="Repair failed CI",
            repo_url="https://github.com/natanayalo/code-agent",
            repair_for_task_id=source_snapshot.task_id,
        )
    )

    assert repair_snapshot.repair_for_task_id == source_snapshot.task_id
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(persisted.task_id)
        assert task is not None
        assert task.repair_for_task_id == source_snapshot.task_id


def test_create_task_rejects_invalid_scout_budget() -> None:
    """Scout task submission with invalid budget should raise ValueError."""
    service, session_factory = _make_task_service()

    submission_negative = execution_module.TaskSubmission(
        task_text="Run a scout task",
        repo_url="https://github.com/natanayalo/code-agent",
        constraints={"task_type": "scout"},
        budget={"max_iterations": -1},
    )

    with pytest.raises(ValueError, match="Budget value for max_iterations cannot be negative"):
        service.create_task(submission_negative)

    submission_invalid_type = execution_module.TaskSubmission(
        task_text="Run a scout task",
        repo_url="https://github.com/natanayalo/code-agent",
        constraints={"task_type": "scout"},
        budget={"max_iterations": "abc"},
    )

    with pytest.raises(ValueError, match="Invalid budget configuration for max_iterations: abc"):
        service.create_task(submission_invalid_type)

    submission_boolean = execution_module.TaskSubmission(
        task_text="Run a scout task",
        repo_url="https://github.com/natanayalo/code-agent",
        constraints={"task_type": "scout"},
        budget={"max_iterations": True},
    )

    with pytest.raises(ValueError, match="Invalid budget configuration for max_iterations: True"):
        service.create_task(submission_boolean)

    submission_inf = execution_module.TaskSubmission(
        task_text="Run a scout task",
        repo_url="https://github.com/natanayalo/code-agent",
        constraints={"task_type": "scout"},
        budget={"max_iterations": "inf"},
    )

    with pytest.raises(ValueError, match="Invalid budget configuration for max_iterations: inf"):
        service.create_task(submission_inf)


@pytest.mark.anyio
async def test_submit_task_emits_progress_notifications_for_success(monkeypatch) -> None:
    """Successful task execution should emit started, running, and completed updates."""
    _, session_factory = _make_task_service()

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
        attempt_count=0,
    )

    async def run_blocking(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module._PersistedTaskContext,
    ) -> OrchestratorState:
        return _make_orchestrator_state_from_persisted(
            _submission,
            _persisted,
            WorkerResult(status="success", summary="all done"),
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
    monkeypatch.setattr(
        service,
        "_persist_execution_outcome",
        lambda **kwargs: SimpleNamespace(
            task_id=persisted.task_id,
            session_id=persisted.session_id,
            task_constraints=None,
            worker_run_id="run-1",
        ),
    )
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
    service, session_factory = _make_task_service()
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
        return _make_orchestrator_state_from_persisted(
            _submission,
            _persisted,
            WorkerResult(status="success", summary="orchestrator finished"),
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
    service, session_factory = _make_task_service()
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
        attempt_count=0,
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
    _, session_factory = _make_task_service()

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
        attempt_count=0,
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


@pytest.mark.anyio
async def test_emit_progress_swallows_notifier_failures(caplog) -> None:
    """Best-effort progress notification failures should never bubble into task execution."""

    class _FailingNotifier:
        async def notify(self, **kwargs) -> None:
            raise RuntimeError("progress notifier boom")

    service, _ = _make_task_service()
    service.progress_notifier = _FailingNotifier()

    with caplog.at_level(logging.WARNING, logger="orchestrator.execution"):
        await service._emit_progress(
            execution_module.TaskSubmission(task_text="Notify operator"),
            execution_module._PersistedTaskContext(
                user_id="user-1",
                session_id="session-1",
                channel="http",
                external_thread_id="thread-1",
                task_id="task-1",
                attempt_count=1,
            ),
            phase="running",
            summary="still working",
        )

    assert "Progress notification failed" in caplog.text
