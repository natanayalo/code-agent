# ruff: noqa: F403, F405
"""Behavior-focused task execution service tests."""

from __future__ import annotations

from db.enums import OrchestrationRuntime
from repositories import WorkerNodeRepository
from tests.unit.task_execution_service_support import *  # noqa: F403


def _build_fake_orchestrator_state(
    submitted: execution_module.TaskSubmission,
    persisted: execution_module.TaskSnapshot,
    current_step: str,
    result_status: str,
    result_summary: str,
    requires_approval: bool = False,
    approval_status: str = "pending",
    failure_kind: str | None = None,
    next_action_hint: str | None = None,
) -> OrchestratorState:
    return OrchestratorState(
        current_step=current_step,
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
            task_text=submitted.task_text,
            repo_url=submitted.repo_url,
            branch=submitted.branch,
            priority=submitted.priority,
            worker_override=submitted.worker_override,
            constraints={"requires_approval": True} if requires_approval else {},
            budget={},
        ),
        normalized_task_text=submitted.task_text,
        task_kind="implementation",
        memory=MemoryContext(),
        route=RouteDecision(
            chosen_worker="codex",
            route_reason="cheap_mechanical_change",
            override_applied=False,
        ),
        approval=ApprovalCheckpoint(
            required=requires_approval, status=approval_status, approval_type="manual_approval"
        )
        if requires_approval
        else ApprovalCheckpoint(),
        dispatch=WorkerDispatch(worker_type="codex"),
        result=WorkerResult(
            status=result_status,
            summary=result_summary,
            failure_kind=failure_kind,
            next_action_hint=next_action_hint,
        ),
    )


async def _idle_heartbeat_loop(*, task_id: str, worker_id: str, lease_seconds: int) -> None:
    """Keep heartbeat alive until run_queued_task cancels it after result handling."""
    await asyncio.Event().wait()


def test_claim_next_task_allows_single_claim_and_lease_reclaim() -> None:
    """Only one worker should claim a pending task until the lease expires."""
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
    submission = execution_module.TaskSubmission(task_text="claim me")
    snapshot, _ = service.create_task(submission)

    first_claim = service.claim_next_task(worker_id="worker-a", lease_seconds=60)
    assert first_claim is not None
    assert first_claim.task_id == snapshot.task_id
    assert first_claim.attempt_count == 1
    assert service.claim_next_task(worker_id="worker-b", lease_seconds=60) is None

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.lease_expires_at is not None
        task.lease_expires_at = task.lease_expires_at - timedelta(seconds=120)
        import datetime

        TaskRepository(session).reclaim_expired_leases(now=datetime.datetime.now(datetime.UTC))

    reclaimed = service.claim_next_task(worker_id="worker-b", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.task_id == snapshot.task_id
    assert reclaimed.attempt_count == 2


@pytest.mark.parametrize("runtime", [OrchestrationRuntime.TEMPORAL, None])
def test_run_queued_task_releases_invalid_nonlegacy_stale_lease(runtime, monkeypatch) -> None:
    """Ownership rejection must release stale non-legacy leases before validation."""
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

    with session_scope(session_factory) as session:
        conversation_session = SessionRepository(session).create(
            user_id="runtime-owner", channel="test", external_thread_id="runtime-thread"
        )
        task = TaskRepository(session).create(
            session_id=conversation_session.id,
            task_text="invalid stale lease",
            constraints={"worker_profile_override": 42},
            orchestration_runtime=runtime,
        )
        task.status = TaskStatus.IN_PROGRESS
        task.lease_owner = "worker-runtime"
        task.lease_expires_at = utc_now()
        task.attempt_count = 1
        worker = WorkerNodeRepository(session).register_worker(
            worker_id="worker-runtime", worker_type="codex", now=utc_now(), capacity=1
        )
        worker.current_load = 1

    def fail_submission_load(*_args, **_kwargs):
        raise AssertionError("non-legacy task must not be validated by the legacy worker")

    monkeypatch.setattr(service, "_load_submission_for_task", fail_submission_load)
    asyncio.run(service.run_queued_task(task_id=task.id, worker_id="worker-runtime"))

    with session_scope(session_factory) as session:
        stored_task = TaskRepository(session).get(task.id)
        worker = WorkerNodeRepository(session).get_by_worker_id("worker-runtime")
        assert stored_task is not None
        assert worker is not None
        assert stored_task.status is TaskStatus.PENDING
        assert stored_task.attempt_count == 0
        assert stored_task.lease_owner is None
        assert worker.current_load == 0


def test_claim_next_task_orders_by_queue_lane_then_priority_then_age() -> None:
    """Queue should yield primary before scout, then by priority, then by age."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    with session_scope(session_factory) as session:
        repo = TaskRepository(session)
        now = utc_now()
        SessionRepository(session).create(
            user_id="u1", channel="test", external_thread_id="t1", last_seen_at=now
        )
        repo.create(
            session_id="t1",
            task_text="scout old",
            queue_lane="scout",
            priority=1,
            next_attempt_at=now - timedelta(minutes=5),
            orchestration_runtime=OrchestrationRuntime.LEGACY,
        )
        repo.create(
            session_id="t1",
            task_text="primary low prio",
            queue_lane="primary",
            priority=0,
            next_attempt_at=now - timedelta(minutes=4),
            orchestration_runtime=OrchestrationRuntime.LEGACY,
        )
        repo.create(
            session_id="t1",
            task_text="primary high prio old",
            queue_lane="primary",
            priority=1,
            next_attempt_at=now - timedelta(minutes=3),
            orchestration_runtime=OrchestrationRuntime.LEGACY,
        )
        repo.create(
            session_id="t1",
            task_text="primary high prio new",
            queue_lane="primary",
            priority=1,
            next_attempt_at=now - timedelta(minutes=2),
            orchestration_runtime=OrchestrationRuntime.LEGACY,
        )

    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_StaticWorker(),
    )
    service.register_worker_node(worker_id="w1", capacity=4)

    claims = []
    for _ in range(4):
        claim = service.claim_next_task(worker_id="w1", lease_seconds=60)
        assert claim is not None
        with session_scope(session_factory) as session:
            task = TaskRepository(session).get(claim.task_id)
            claims.append(task)

    assert claims[0].task_text == "primary high prio old"
    assert claims[1].task_text == "primary high prio new"
    assert claims[2].task_text == "primary low prio"
    assert claims[3].task_text == "scout old"

    assert service.claim_next_task(worker_id="w1", lease_seconds=60) is None


def test_release_failure_requeues_until_max_attempts_then_fails() -> None:
    """Failed attempts should requeue until max attempts, then become terminally failed."""
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
        default_task_max_attempts=2,
    )
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="retry me"))

    claim_one = service.claim_next_task(worker_id="worker-a", lease_seconds=60)
    assert claim_one is not None
    service._release_task_failure(task_id=snapshot.task_id, worker_id="worker-a")

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.next_attempt_at is not None

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        task.next_attempt_at = utc_now() - timedelta(seconds=1)

    claim_two = service.claim_next_task(worker_id="worker-a", lease_seconds=60)
    assert claim_two is not None
    service._release_task_failure(task_id=snapshot.task_id, worker_id="worker-a")

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED


def test_run_queued_task_requeues_failed_result_when_retries_remain(monkeypatch) -> None:
    """Queued execution should preserve retryability for worker-declared failures."""
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
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires retry",
            repo_url="https://github.com/natanayalo/code-agent",
        )
    )
    with session_scope(session_factory) as session:
        HumanInteractionRepository(session).sync_task_spec_flags(
            task_id=snapshot.task_id,
            task_spec={
                "requires_permission": True,
                "permission_reason": "Manual approval required.",
                "risk_level": "high",
            },
        )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return _build_fake_orchestrator_state(
            submitted=_submission,
            persisted=persisted,
            current_step="await_result",
            result_status="failure",
            result_summary="Simulated failure should be retried.",
        )

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", _idle_heartbeat_loop)

    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    assert [event.phase for event in notifier.events] == ["started", "running", "failed"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.attempt_count == 1
        assert task.next_attempt_at is not None
        assert task.lease_owner is None
        assert task.lease_expires_at is None


def test_run_queued_task_only_quarantines_provider_and_infra_failures(monkeypatch) -> None:
    """Queued runtime should not quarantine workers for normal test failures."""
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
    service.register_worker_node(worker_id="worker-health", capacity=1)

    monkeypatch.setattr(service, "_heartbeat_loop", _idle_heartbeat_loop)

    async def fake_test_failure(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return _build_fake_orchestrator_state(
            submitted=submitted,
            persisted=persisted,
            current_step="await_result",
            result_status="failure",
            result_summary="Targeted tests failed.",
            failure_kind="test",
        )

    monkeypatch.setattr(service, "_run_orchestrator", fake_test_failure)
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="test failure"))
    claim = service.claim_next_task(worker_id="worker-health", lease_seconds=45)
    assert claim is not None
    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-health"))

    with session_scope(session_factory) as session:
        node = WorkerNodeRepository(session).get_by_worker_id("worker-health")
        assert node is not None
        assert node.status.value == "active"
        assert node.consecutive_failures == 0

    async def fake_provider_failure(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return _build_fake_orchestrator_state(
            submitted=submitted,
            persisted=persisted,
            current_step="await_result",
            result_status="error",
            result_summary="Provider returned quota exhausted.",
            failure_kind="provider_error",
        )

    monkeypatch.setattr(service, "_run_orchestrator", fake_provider_failure)
    for index in range(3):
        snapshot, _ = service.create_task(
            execution_module.TaskSubmission(task_text=f"provider failure {index}")
        )
        claim = service.claim_next_task(worker_id="worker-health", lease_seconds=45)
        assert claim is not None
        asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-health"))

    with session_scope(session_factory) as session:
        node = WorkerNodeRepository(session).get_by_worker_id("worker-health")
        assert node is not None
        assert node.status.value == "quarantined"
        assert node.consecutive_failures == 3
        assert node.quarantine_reason is not None


def test_run_queued_task_wraps_execution_in_restored_trace_context(monkeypatch) -> None:
    """Queued runs should always execute within the restored trace context scope."""
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
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="fails but detaches trace context",
            repo_url="https://github.com/natanayalo/code-agent",
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        _submission: execution_module.TaskSubmission,
        _persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        raise RuntimeError("boom")

    async def run_blocking(func, *args, **kwargs):
        return func(*args, **kwargs)

    captured_contexts: list[dict[str, str] | None] = []
    scope_events = {"entered": 0, "exited": 0}

    @contextmanager
    def fake_restored_trace_context(context: dict[str, str] | None):
        captured_contexts.append(context)
        scope_events["entered"] += 1
        try:
            yield
        finally:
            scope_events["exited"] += 1

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", _idle_heartbeat_loop)
    monkeypatch.setattr(service, "_run_blocking", run_blocking)
    monkeypatch.setattr(
        execution_module,
        "with_restored_trace_context",
        fake_restored_trace_context,
    )

    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))
    assert len(captured_contexts) == 1
    assert scope_events == {"entered": 1, "exited": 1}


def test_heartbeat_task_lease_uses_configured_duration(monkeypatch) -> None:
    """Lease heartbeat should extend by the worker-configured lease duration."""
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
    snapshot, _ = service.create_task(execution_module.TaskSubmission(task_text="heartbeat me"))
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=30)
    assert claim is not None

    captured: dict[str, int] = {}
    original_heartbeat = TaskRepository.heartbeat_lease

    def recording_heartbeat(
        self: TaskRepository,
        *,
        task_id: str,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        captured["lease_seconds"] = lease_seconds
        return original_heartbeat(
            self,
            task_id=task_id,
            worker_id=worker_id,
            now=now,
            lease_seconds=lease_seconds,
        )

    monkeypatch.setattr(TaskRepository, "heartbeat_lease", recording_heartbeat)

    assert service._heartbeat_task_lease(
        task_id=snapshot.task_id,
        worker_id="worker-a",
        lease_seconds=123,
    )
    assert captured["lease_seconds"] == 123


def test_heartbeat_interval_seconds_tracks_lease_duration() -> None:
    """Heartbeat interval should scale with lease and stay inside safe bounds."""
    assert execution_module._heartbeat_interval_seconds(lease_seconds=3) == 1.0
    assert execution_module._heartbeat_interval_seconds(lease_seconds=30) == 10.0
    assert execution_module._heartbeat_interval_seconds(lease_seconds=90) == 10.0
    assert execution_module._heartbeat_interval_seconds(lease_seconds=1) == 1.0


def test_run_queued_task_terminal_interrupt_emits_awaiting_approval_without_requeue(
    monkeypatch,
) -> None:
    """Manual-follow-up failures should stay terminal instead of requeueing."""
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
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires manual approval",
            repo_url="https://github.com/natanayalo/code-agent",
            constraints={"requires_approval": True},
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return _build_fake_orchestrator_state(
            submitted=submitted,
            persisted=persisted,
            current_step="await_approval",
            result_status="failure",
            result_summary="Run paused pending manual approval approval.",
            requires_approval=True,
            next_action_hint="await_manual_follow_up",
        )

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", _idle_heartbeat_loop)

    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    assert [event.phase for event in notifier.events] == [
        "started",
        "running",
        "awaiting_approval",
    ]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.next_attempt_at is None
        assert task.lease_owner is None
        assert task.lease_expires_at is None


def test_run_queued_task_rejected_approval_stays_failed(monkeypatch) -> None:
    """Explicit approval rejection should remain terminally failed, not pending."""
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
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires manual approval",
            repo_url="https://github.com/natanayalo/code-agent",
            constraints={"requires_approval": True},
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return _build_fake_orchestrator_state(
            submitted=submitted,
            persisted=persisted,
            current_step="await_approval",
            result_status="failure",
            result_summary="Task halted because the requested destructive action was not approved.",
            requires_approval=True,
            approval_status="rejected",
            failure_kind="permission_denied",
            next_action_hint="await_manual_follow_up",
        )

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", _idle_heartbeat_loop)

    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    assert [event.phase for event in notifier.events] == ["started", "running", "failed"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED
        assert task.next_attempt_at is None
        assert task.lease_owner is None
        assert task.lease_expires_at is None
        approval = dict(task.constraints).get("approval")
        assert isinstance(approval, dict)
        assert approval.get("status") == "rejected"


def test_apply_task_approval_decision_requeues_approved_task(monkeypatch) -> None:
    """Approving a paused task should move it back to pending for queue pickup."""
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
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires manual approval",
            repo_url="https://github.com/natanayalo/code-agent",
            constraints={"requires_approval": True},
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return _build_fake_orchestrator_state(
            submitted=submitted,
            persisted=persisted,
            current_step="await_approval",
            result_status="failure",
            result_summary="Run paused pending manual approval.",
            requires_approval=True,
            next_action_hint="await_manual_follow_up",
        )

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", _idle_heartbeat_loop)
    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    decision = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=True)
    assert decision.status == "applied"
    assert decision.task_snapshot is not None
    assert decision.task_snapshot.status == TaskStatus.PENDING.value

    duplicate = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=True)
    assert duplicate.status == "already_applied"

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.PENDING
        assert task.next_attempt_at is not None
        assert task.lease_owner is None
        approval = dict(task.constraints).get("approval")
        assert isinstance(approval, dict)
        assert approval.get("status") == "approved"
        assert approval.get("approved") is True


def test_apply_task_approval_decision_reject_is_terminal_and_conflict_is_reported(
    monkeypatch,
) -> None:
    """Rejected decisions stay terminal and opposite follow-up decisions are blocked."""
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
    snapshot, _ = service.create_task(
        execution_module.TaskSubmission(
            task_text="requires manual approval",
            repo_url="https://github.com/natanayalo/code-agent",
            constraints={"requires_approval": True},
        )
    )
    claim = service.claim_next_task(worker_id="worker-a", lease_seconds=45)
    assert claim is not None

    async def fake_run_orchestrator(
        submitted: execution_module.TaskSubmission,
        persisted: execution_module.TaskSnapshot,
    ) -> OrchestratorState:
        return _build_fake_orchestrator_state(
            submitted=submitted,
            persisted=persisted,
            current_step="await_approval",
            result_status="failure",
            result_summary="Run paused pending manual approval.",
            requires_approval=True,
            next_action_hint="await_manual_follow_up",
        )

    monkeypatch.setattr(service, "_run_orchestrator", fake_run_orchestrator)
    monkeypatch.setattr(service, "_heartbeat_loop", _idle_heartbeat_loop)
    asyncio.run(service.run_queued_task(task_id=snapshot.task_id, worker_id="worker-a"))

    rejected = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=False)
    assert rejected.status == "applied"
    assert rejected.task_snapshot is not None
    assert rejected.task_snapshot.status == TaskStatus.FAILED.value
    assert rejected.task_snapshot.latest_run is not None
    assert "rejected" in (rejected.task_snapshot.latest_run.summary or "").lower()

    duplicate = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=False)
    assert duplicate.status == "already_applied"

    conflict = service.apply_task_approval_decision(task_id=snapshot.task_id, approved=True)
    assert conflict.status == "conflict"

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(snapshot.task_id)
        assert task is not None
        assert task.status is TaskStatus.FAILED
        assert task.next_attempt_at is None
        approval = dict(task.constraints).get("approval")
        assert isinstance(approval, dict)
        assert approval.get("status") == "rejected"
        assert approval.get("approved") is False
        interactions = HumanInteractionRepository(session).list_by_task(task_id=task.id)
        assert len(interactions) == 1
        assert interactions[0].status == HumanInteractionStatus.REJECTED
        assert interactions[0].response_data == {
            "approved": False,
            "source": "api",
            "reason": interactions[0].summary,
        }
