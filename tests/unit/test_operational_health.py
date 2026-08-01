"""Unit tests for dependency readiness and reconciliation policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Response
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool
from starlette.requests import Request
from temporalio.client import WorkflowExecutionStatus

from apps.api.routes import health as health_routes
from db.base import Base
from db.enums import (
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    OrchestrationRuntime,
    TaskStatus,
    WorkerNodeStatus,
)
from db.models import HumanInteraction, Task, TemporalCommand
from orchestrator import operational_health
from orchestrator.execution import TaskExecutionService, TemporalUnavailableError
from orchestrator.operational_health_types import ReadinessComponent, ReadinessSnapshot
from repositories import (
    SessionRepository,
    TaskRepository,
    UserRepository,
    WorkerNodeRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from repositories.sqlalchemy_operational_health import (
    InteractionOperationalSnapshot,
    OperationalHealthRepository,
    OutboxOperationalSnapshot,
    TemporalTaskProjection,
    WorkerOperationalSnapshot,
    _age_seconds,
    _as_aware,
)


class FakeTemporalProbe:
    def __init__(
        self,
        *,
        available: bool = True,
        statuses: dict[str, str] | None = None,
    ) -> None:
        self.available = available
        self.statuses = statuses or {}
        self.availability_calls = 0

    def is_available(self) -> bool:
        self.availability_calls += 1
        return self.available

    def list_task_workflow_statuses(self) -> dict[str, str]:
        if not self.available:
            raise ConnectionError("unavailable")
        return dict(self.statuses)


def _snapshots(*, eligible_age: float | None = None):
    return (
        OutboxOperationalSnapshot(
            pending_count=0,
            retrying_count=0,
            dead_letter_count=0,
            oldest_unresolved_age_seconds=eligible_age,
            oldest_eligible_age_seconds=eligible_age,
            affected_task_ids=[],
        ),
        WorkerOperationalSnapshot(
            fresh_count=1,
            stale_count=0,
            fresh_dispatcher_count=1,
            freshest_heartbeat_at=datetime(2026, 8, 1, tzinfo=UTC),
            freshest_heartbeat_age_seconds=0,
            freshest_dispatcher_heartbeat_at=datetime(2026, 8, 1, tzinfo=UTC),
            freshest_dispatcher_heartbeat_age_seconds=0,
        ),
        InteractionOperationalSnapshot(
            pending_count=0,
            stuck_count=0,
            oldest_pending_age_seconds=None,
            affected_task_ids=[],
        ),
        {},
    )


def test_readiness_outbox_threshold_is_strictly_greater(monkeypatch) -> None:
    """Exactly 60 seconds remains ready; older eligible work blocks dispatch."""
    service = SimpleNamespace(temporal_operational_probe=FakeTemporalProbe())
    snapshots = {"value": _snapshots(eligible_age=60)}
    monkeypatch.setattr(
        operational_health,
        "_database_snapshots",
        lambda _service: snapshots["value"],
    )

    ready = operational_health.get_readiness(service)
    snapshots["value"] = _snapshots(eligible_age=60.001)
    stale = operational_health.get_readiness(service)

    assert ready.status == "ready"
    assert stale.status == "not_ready"
    assert stale.components["dispatcher"].reasons == ["dispatcher_backlog_stale"]


def test_reconciliation_exercises_grace_and_both_divergence_directions() -> None:
    """Only post-grace workflow/Postgres contradictions are degraded."""
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    old = now - timedelta(minutes=2)
    recent = now - timedelta(seconds=30)
    projections = {
        "terminal-old": TemporalTaskProjection("completed", old, old),
        "terminal-recent": TemporalTaskProjection("completed", old, recent),
        "active-closed": TemporalTaskProjection("in_progress", old, old),
        "active-running": TemporalTaskProjection("in_progress", old, old),
        "active-recent": TemporalTaskProjection("in_progress", recent, old),
    }
    probe = FakeTemporalProbe(
        statuses={
            "terminal-old": "running",
            "terminal-recent": "running",
            "active-closed": "completed",
            "active-running": "running",
            "missing-postgres": "running",
        }
    )
    service = SimpleNamespace(temporal_operational_probe=probe)

    result = operational_health._reconciliation_metrics(
        service,
        checked_at=now,
        projections=projections,
    )

    assert result.status == "degraded"
    assert result.divergence_count == 3
    assert set(result.affected_task_ids) == {
        "terminal-old",
        "active-closed",
        "missing-postgres",
    }


def test_reconciliation_is_unknown_when_temporal_visibility_fails() -> None:
    service = SimpleNamespace(temporal_operational_probe=FakeTemporalProbe(available=False))

    result = operational_health._reconciliation_metrics(
        service,
        checked_at=datetime(2026, 8, 1, tzinfo=UTC),
        projections={},
    )

    assert result.status == "unknown"
    assert result.divergence_count is None


def test_reconciliation_caps_affected_task_ids() -> None:
    statuses = {f"missing-{index:02d}": "running" for index in range(21)}
    service = SimpleNamespace(temporal_operational_probe=FakeTemporalProbe(statuses=statuses))

    result = operational_health._reconciliation_metrics(
        service,
        checked_at=datetime(2026, 8, 1, tzinfo=UTC),
        projections={},
    )

    assert result.divergence_count == 21
    assert len(result.affected_task_ids) == 20
    assert result.affected_task_ids_truncated


def test_temporal_probe_checks_health_and_keeps_latest_running_execution(monkeypatch) -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    healthy = {"value": True}

    class ServiceClient:
        async def check_health(self, **_kwargs) -> bool:
            return healthy["value"]

    class FakeClient:
        service_client = ServiceClient()

        def list_workflows(self, _query):
            async def workflows():
                yield SimpleNamespace(
                    id="task-a",
                    status=WorkflowExecutionStatus.COMPLETED,
                    start_time=now,
                )
                yield SimpleNamespace(
                    id="task-a",
                    status=WorkflowExecutionStatus.RUNNING,
                    start_time=now - timedelta(minutes=1),
                )
                yield SimpleNamespace(id="other", status=None, start_time=now)

            return workflows()

    async def connect(_address: str):
        return FakeClient()

    monkeypatch.setattr(operational_health.Client, "connect", connect)
    probe = operational_health.TemporalOperationalProbe(address="temporal:7233")

    assert probe.is_available()
    assert probe.list_task_workflow_statuses() == {"a": "running"}
    healthy["value"] = False
    assert not probe.is_available()


def test_temporal_probe_returns_unavailable_for_connection_failure(monkeypatch) -> None:
    async def connect(_address: str):
        raise ConnectionError("down")

    monkeypatch.setattr(operational_health.Client, "connect", connect)

    assert not operational_health.TemporalOperationalProbe(address="temporal:7233").is_available()


def test_submission_temporal_probe_retries_and_recovers(monkeypatch) -> None:
    outcomes = iter((False, False, True))

    class SequenceProbe(FakeTemporalProbe):
        def is_available(self) -> bool:
            self.availability_calls += 1
            return next(outcomes)

    probe = SequenceProbe()
    service = SimpleNamespace(temporal_operational_probe=probe)
    monkeypatch.setattr("time.sleep", lambda _delay: None)

    operational_health.ensure_temporal_available(service)

    assert probe.availability_calls == 3


def test_submission_temporal_probe_raises_after_bounded_retries(monkeypatch) -> None:
    probe = FakeTemporalProbe(available=False)
    service = SimpleNamespace(temporal_operational_probe=probe)
    monkeypatch.setattr("time.sleep", lambda _delay: None)

    with pytest.raises(TemporalUnavailableError, match="new tasks are temporarily disabled"):
        operational_health.ensure_temporal_available(service)

    assert probe.availability_calls == 3


def test_execution_health_reports_every_nonblocking_anomaly(monkeypatch) -> None:
    """Authenticated metrics retain anomalies that do not all block readiness."""
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    affected = [f"task-{index:02d}" for index in range(21)]
    snapshots = (
        OutboxOperationalSnapshot(1, 2, 3, 120, 61, affected),
        WorkerOperationalSnapshot(
            fresh_count=0,
            stale_count=2,
            fresh_dispatcher_count=0,
            freshest_heartbeat_at=now - timedelta(seconds=31),
            freshest_heartbeat_age_seconds=31,
            freshest_dispatcher_heartbeat_at=now - timedelta(seconds=40),
            freshest_dispatcher_heartbeat_age_seconds=40,
        ),
        InteractionOperationalSnapshot(4, 1, 90_000, affected),
        {"terminal": TemporalTaskProjection("completed", now, now - timedelta(minutes=2))},
    )
    probe = FakeTemporalProbe(statuses={"terminal": "running"})
    service = SimpleNamespace(temporal_operational_probe=probe)
    monkeypatch.setattr(operational_health, "utc_now", lambda: now)
    monkeypatch.setattr(operational_health, "_database_snapshots", lambda _service: snapshots)

    metrics = operational_health.get_execution_health(service)

    assert metrics.degraded_reasons == [
        "command_retries_present",
        "command_dead_letters_present",
        "dispatcher_backlog_stale",
        "worker_unavailable",
        "dispatcher_unavailable",
        "interaction_wait_stuck",
        "terminal_state_divergence",
    ]
    assert metrics.outbox.affected_task_ids == affected[:20]
    assert metrics.outbox.affected_task_ids_truncated
    assert metrics.interactions.affected_task_ids_truncated

    probe.available = False
    unknown = operational_health.get_execution_health(service)
    assert unknown.reconciliation.status == "unknown"
    assert unknown.degraded_reasons[-1] == "terminal_reconciliation_unknown"


def test_execution_health_reports_healthy_empty_state(monkeypatch) -> None:
    service = SimpleNamespace(temporal_operational_probe=FakeTemporalProbe())
    monkeypatch.setattr(operational_health, "_database_snapshots", lambda _service: _snapshots())

    metrics = operational_health.get_execution_health(service)

    assert metrics.degraded_reasons == []
    assert metrics.reconciliation.status == "ok"


def test_default_temporal_probe_uses_environment_address(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal.internal:7233")
    monkeypatch.setattr(
        operational_health,
        "TemporalOperationalProbe",
        lambda *, address: sentinel if address == "temporal.internal:7233" else None,
    )

    assert operational_health._temporal_probe(SimpleNamespace()) is sentinel


def test_readiness_sanitizes_database_and_temporal_failures(monkeypatch) -> None:
    """Dependency failures produce stable codes without exposing exceptions."""
    service = SimpleNamespace(temporal_operational_probe=FakeTemporalProbe(available=False))

    def fail_database(_service):
        raise RuntimeError("secret connection details")

    monkeypatch.setattr(operational_health, "_database_snapshots", fail_database)

    snapshot = operational_health.get_readiness(service)

    assert snapshot.status == "not_ready"
    assert snapshot.degraded_reasons == ["postgres_unavailable", "temporal_unavailable"]
    assert list(snapshot.components) == ["postgres", "temporal", "worker", "dispatcher"]
    assert snapshot.components["worker"].status == "unknown"
    assert "secret connection details" not in snapshot.model_dump_json()


def test_readiness_uses_dispatcher_specific_heartbeat_timestamp(monkeypatch) -> None:
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    dispatcher_heartbeat = now - timedelta(seconds=31)
    snapshots = (
        _snapshots()[0],
        WorkerOperationalSnapshot(
            fresh_count=1,
            stale_count=1,
            fresh_dispatcher_count=0,
            freshest_heartbeat_at=now,
            freshest_heartbeat_age_seconds=0,
            freshest_dispatcher_heartbeat_at=dispatcher_heartbeat,
            freshest_dispatcher_heartbeat_age_seconds=31,
        ),
        _snapshots()[2],
        {},
    )
    monkeypatch.setattr(operational_health, "_database_snapshots", lambda _service: snapshots)

    readiness = operational_health.get_readiness(
        SimpleNamespace(temporal_operational_probe=FakeTemporalProbe())
    )

    assert readiness.components["worker"].last_observed_at == now
    assert readiness.components["dispatcher"].last_observed_at == dispatcher_heartbeat


def test_health_route_unit_boundaries(monkeypatch) -> None:
    """The public routes keep liveness simple and readiness structured."""
    assert health_routes.health().model_dump() == {"status": "ok"}
    request = Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(task_service=None))}
    )
    response = Response()

    unconfigured = health_routes.ready(request, response)

    assert response.status_code == 503
    assert unconfigured.degraded_reasons == ["task_service_unconfigured"]

    service = object.__new__(TaskExecutionService)
    expected = ReadinessSnapshot(
        status="ready",
        checked_at=datetime(2026, 8, 1, tzinfo=UTC),
        components={
            name: ReadinessComponent(status="ready")
            for name in ("postgres", "temporal", "worker", "dispatcher")
        },
    )
    monkeypatch.setattr(TaskExecutionService, "get_readiness", lambda _self: expected)
    request = Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(task_service=service))}
    )
    response = Response()

    configured = health_routes.ready(request, response)

    assert configured is expected
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def _create_operational_tasks(session: OrmSession, *, old: datetime) -> tuple[Task, ...]:
    user = UserRepository(session).create(external_user_id="health-user")
    conversation = SessionRepository(session).create(
        user_id=user.id,
        channel="test",
        external_thread_id="health-thread",
    )
    task_repo = TaskRepository(session)
    pending = task_repo.create(
        session_id=conversation.id,
        task_text="pending",
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
    )
    retrying = task_repo.create(
        session_id=conversation.id,
        task_text="retrying",
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
    )
    dead = task_repo.create(session_id=conversation.id, task_text="dead")
    terminal = task_repo.create(
        session_id=conversation.id,
        task_text="terminal",
        status=TaskStatus.COMPLETED,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
    )
    terminal.updated_at = old
    return pending, retrying, dead, terminal


def _add_operational_commands(
    session: OrmSession,
    *,
    tasks: tuple[Task, ...],
    now: datetime,
    old: datetime,
) -> None:
    pending, retrying, dead, terminal = tasks
    session.add_all(
        [
            TemporalCommand(
                task_id=pending.id,
                command_type="start",
                sequence_number=1,
                command_key="pending-start",
                payload={},
                created_at=now - timedelta(seconds=90),
                next_attempt_at=old,
            ),
            TemporalCommand(
                task_id=retrying.id,
                command_type="start",
                sequence_number=1,
                command_key="retry-start",
                payload={},
                attempts=2,
                created_at=old,
                next_attempt_at=now + timedelta(minutes=1),
            ),
            TemporalCommand(
                task_id=dead.id,
                command_type="start",
                sequence_number=1,
                command_key="dead-start",
                payload={},
                dead_lettered_at=old,
                created_at=old,
                next_attempt_at=old,
            ),
            TemporalCommand(
                task_id=terminal.id,
                command_type="start",
                sequence_number=1,
                command_key="delivered-start",
                payload={},
                delivered_at=old,
                next_attempt_at=old,
            ),
        ]
    )


def _add_operational_workers(session: OrmSession, *, now: datetime) -> None:
    worker_repo = WorkerNodeRepository(session)
    worker_repo.register_worker(
        worker_id="dispatcher",
        worker_type="codex",
        now=now - timedelta(seconds=30),
        capabilities={"command_dispatcher": True},
    )
    worker_repo.register_worker(
        worker_id="stale",
        worker_type="codex",
        now=now - timedelta(seconds=31),
    )
    quarantined = worker_repo.register_worker(
        worker_id="quarantined",
        worker_type="codex",
        now=now,
    )
    quarantined.status = WorkerNodeStatus.QUARANTINED


def _add_operational_interactions(
    session: OrmSession, *, tasks: tuple[Task, ...], now: datetime
) -> None:
    pending, retrying, _, terminal = tasks
    session.add_all(
        [
            HumanInteraction(
                task_id=pending.id,
                interaction_type=HumanInteractionType.CLARIFICATION,
                status=HumanInteractionStatus.PENDING,
                hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                summary="stuck",
                created_at=now - timedelta(hours=25),
            ),
            HumanInteraction(
                task_id=terminal.id,
                interaction_type=HumanInteractionType.CLARIFICATION,
                status=HumanInteractionStatus.PENDING,
                hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                summary="terminal task ignored",
                created_at=now - timedelta(hours=25),
            ),
            HumanInteraction(
                task_id=retrying.id,
                interaction_type=HumanInteractionType.CLARIFICATION,
                status=HumanInteractionStatus.RESOLVED,
                hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                summary="answered",
            ),
        ]
    )


def test_operational_repository_reports_mixed_state() -> None:
    """Repository aggregates preserve exact counts, ages, and task projections."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    old = now - timedelta(seconds=120)
    with session_scope(session_factory) as session:
        tasks = _create_operational_tasks(session, old=old)
        _add_operational_commands(session, tasks=tasks, now=now, old=old)
        _add_operational_workers(session, now=now)
        _add_operational_interactions(session, tasks=tasks, now=now)
        session.flush()
        repo = OperationalHealthRepository(session)
        repo.ping(timeout_seconds=2)
        outbox = repo.outbox_snapshot(now=now, task_id_limit=20)
        workers = repo.worker_snapshot(now=now, fresh_seconds=30)
        interactions = repo.interaction_snapshot(
            now=now, stuck_seconds=24 * 60 * 60, task_id_limit=20
        )
        projections = repo.temporal_task_projections()
        pending_id, retrying_id, dead_id, terminal_id = (task.id for task in tasks)

    combined = operational_health._database_snapshots(
        SimpleNamespace(session_factory=session_factory)
    )

    assert (outbox.pending_count, outbox.retrying_count, outbox.dead_letter_count) == (1, 1, 1)
    assert outbox.oldest_unresolved_age_seconds == 120
    assert outbox.oldest_eligible_age_seconds == 90
    assert set(outbox.affected_task_ids) == {pending_id, retrying_id, dead_id}
    assert (workers.fresh_count, workers.stale_count, workers.fresh_dispatcher_count) == (1, 1, 1)
    assert workers.freshest_heartbeat_age_seconds == 0
    assert workers.freshest_dispatcher_heartbeat_age_seconds == 30
    assert interactions.pending_count == interactions.stuck_count == 1
    assert interactions.oldest_pending_age_seconds == 25 * 60 * 60
    assert interactions.affected_task_ids == [pending_id]
    assert projections[terminal_id].status == "completed"
    assert projections[terminal_id].start_delivered_at == old
    assert combined[0].pending_count == 1
    assert combined[3][terminal_id].status == "completed"


def test_operational_repository_helpers_and_empty_state() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    assert _as_aware(None) is None
    assert _as_aware(now) is now
    assert _as_aware(now.replace(tzinfo=None)) == now
    assert _age_seconds(now, None) is None
    assert _age_seconds(now, now + timedelta(seconds=1)) == 0


def test_postgres_ping_sets_local_statement_timeout() -> None:
    statements: list[str] = []

    class ScalarResult:
        def scalar_one(self) -> int:
            return 1

    class PostgresSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def scalar(self, statement) -> str:
            statements.append(str(statement))
            return "2000"

        def execute(self, statement) -> ScalarResult:
            statements.append(str(statement))
            return ScalarResult()

    repo = OperationalHealthRepository(PostgresSession())  # type: ignore[arg-type]

    repo.ping(timeout_seconds=2)

    assert "set_config" in statements[0]
    assert statements[1] == "SELECT 1"


def test_execution_service_registers_and_heartbeats_dispatcher_owner() -> None:
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=SimpleNamespace(),  # type: ignore[arg-type]
    )

    status = service.register_worker_node(
        worker_id="temporal-worker",
        capacity=2,
        process_identity="host:42",
    )
    heartbeat_status = service.heartbeat_worker_node(worker_id="temporal-worker")

    assert status is heartbeat_status is WorkerNodeStatus.ACTIVE
    with session_scope(session_factory) as session:
        node = WorkerNodeRepository(session).get_by_worker_id("temporal-worker")
        assert node is not None
        assert node.capacity == 2
        assert node.process_identity == "host:42"
        assert node.capabilities["runtime"] == "temporal"
        assert node.capabilities["command_dispatcher"] is True
