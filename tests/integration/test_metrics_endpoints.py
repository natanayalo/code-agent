"""Integration tests for the operational metrics API."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from apps.api.auth import DASHBOARD_COOKIE_NAME, ApiAuthConfig, create_dashboard_token
from apps.api.main import create_app
from apps.runtime import initialize_persisted_cutover
from db.base import Base, utc_now
from db.enums import (
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    OrchestrationRuntime,
    TaskStatus,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import HumanInteraction, TemporalCommand
from orchestrator.execution import TaskExecutionService
from repositories import (
    TaskRepository,
    TemporalCommandRepository,
    WorkerNodeRepository,
    WorkerRunRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


class StaticWorker(Worker):
    """Worker double that returns a predefined result."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result

    async def run(self, request: WorkerRequest) -> WorkerResult:
        return self.result


class MutableTemporalProbe:
    """Controllable Temporal visibility probe for metrics assertions."""

    def __init__(self) -> None:
        self.available = True
        self.workflow_statuses: dict[str, str] = {}

    def is_available(self) -> bool:
        return self.available

    def list_task_workflow_statuses(self) -> dict[str, str]:
        if not self.available:
            raise ConnectionError("Temporal unavailable")
        return dict(self.workflow_statuses)


@pytest.fixture
def session_factory():
    """Create a SQLite-backed session factory for metrics tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    """Provide a test client with metrics route and auth configured."""
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="ok",
            budget_usage={},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )
    )
    probe = MutableTemporalProbe()
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=worker,
            temporal_operational_probe=probe,
        ),
        auth_config=ApiAuthConfig(shared_secret=("a" * 32)),  # gitleaks:allow
    )
    app.state.test_temporal_probe = probe
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = (
            "a" * 32  # gitleaks:allow
        )
        yield test_client


def test_get_metrics_requires_auth(session_factory) -> None:
    """The metrics endpoint must reject unauthenticated requests."""
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=StaticWorker(
                WorkerResult(
                    status="success",
                    summary="ok",
                    budget_usage={},
                    commands_run=[],
                    files_changed=[],
                    artifacts=[],
                    next_action_hint=None,
                )
            ),
        ),
        auth_config=ApiAuthConfig(shared_secret=("a" * 32)),  # gitleaks:allow
    )
    with TestClient(app) as client:
        # No header
        response = client.get("/metrics")
        assert response.status_code == 401

        # Wrong header
        response = client.get("/metrics", headers={"X-Webhook-Token": "wrong"})
        assert response.status_code == 403


def test_get_metrics_allows_cookie_auth(session_factory) -> None:
    """The metrics endpoint should allow authentication via dashboard session cookie."""
    shared_secret = "a" * 32  # gitleaks:allow
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=StaticWorker(
                WorkerResult(
                    status="success",
                    summary="ok",
                    budget_usage={},
                    commands_run=[],
                    files_changed=[],
                    artifacts=[],
                    next_action_hint=None,
                )
            ),
        ),
        auth_config=ApiAuthConfig(shared_secret=shared_secret),
    )
    token = create_dashboard_token(shared_secret)

    with TestClient(app) as client:
        # No header, but valid cookie
        client.cookies.set(DASHBOARD_COOKIE_NAME, token)
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "total_tasks" in response.json()


def _seed_aggregated_metrics(task_repo, run_repo, now) -> None:
    """Create a mixed-runtime task and worker-run set for metrics assertions."""
    t1 = task_repo.create(
        session_id="s1",
        task_text="task 1",
        status=TaskStatus.COMPLETED,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
    )
    t1.attempt_count = 1
    t2 = task_repo.create(
        session_id="s1",
        task_text="task 2",
        status=TaskStatus.FAILED,
        orchestration_runtime=OrchestrationRuntime.LEGACY,
    )
    t2.attempt_count = 1
    task_repo.create(session_id="s2", task_text="task 3", status=TaskStatus.PENDING)
    t4 = task_repo.create(
        session_id="s2",
        task_text="task 4",
        status=TaskStatus.COMPLETED,
        orchestration_runtime=OrchestrationRuntime.LEGACY,
    )
    t4.attempt_count = 2
    for task, worker_type, runtime_mode, finished_at, status in (
        (
            t1,
            WorkerType.CODEX,
            WorkerRuntimeMode.NATIVE_AGENT,
            now - timedelta(minutes=5),
            WorkerRunStatus.SUCCESS,
        ),
        (
            t2,
            WorkerType.ANTIGRAVITY,
            WorkerRuntimeMode.NATIVE_AGENT,
            now - timedelta(minutes=2),
            WorkerRunStatus.FAILURE,
        ),
        (
            t4,
            WorkerType.CODEX,
            WorkerRuntimeMode.TOOL_LOOP,
            now - timedelta(minutes=7),
            WorkerRunStatus.SUCCESS,
        ),
    ):
        run_repo.create(
            task_id=task.id,
            worker_type=worker_type,
            runtime_mode=runtime_mode,
            started_at=now - timedelta(minutes=10),
            finished_at=finished_at,
            status=status,
        )


def test_get_metrics_returns_aggregated_stats(client: TestClient, session_factory) -> None:
    """Metrics should reflect the aggregated state of tasks and runs in the DB."""
    now = utc_now()

    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        run_repo = WorkerRunRepository(session)

        _seed_aggregated_metrics(task_repo, run_repo, now)
        session.flush()

    response = client.get("/metrics")
    assert response.status_code == 200
    data = response.json()

    # Task metrics
    assert data["total_tasks"] == 4
    assert data["retried_tasks"] == 1
    # 1 retried out of 3 attempted (t1, t2, t4)
    assert data["retry_rate"] == 1 / 3
    assert data["status_counts"]["completed"] == 2
    assert data["status_counts"]["failed"] == 1
    assert data["status_counts"]["pending"] == 1

    # Run metrics
    assert data["worker_usage"]["codex"] == 2
    assert data["worker_usage"]["antigravity"] == 1
    assert data["runtime_mode_usage"]["native_agent"] == 2
    assert data["runtime_mode_usage"]["tool_loop"] == 1
    assert data["legacy_tool_loop_usage"]["codex"] == 1
    assert data["orchestration_runtime_counts"] == {"temporal": 1, "legacy": 2, "unknown": 1}
    assert data["active_legacy_task_count"] == 0
    assert data["active_unknown_task_count"] == 1
    # Average of 5, 8, and 3 minutes = (300 + 480 + 180) / 3 = 960 / 3 = 320 seconds
    assert data["avg_duration_seconds"] == 320.0
    # 2 successes out of 3 runs
    assert data["success_rate"] == 2 / 3


def test_get_metrics_empty_state(client: TestClient) -> None:
    """Metrics should return sensible defaults when the database is empty."""
    response = client.get("/metrics")
    assert response.status_code == 200
    data = response.json()

    assert data["total_tasks"] == 0
    assert data["retried_tasks"] == 0
    assert data["retry_rate"] == 0.0
    assert data["status_counts"] == {}
    assert data["worker_usage"] == {}
    assert data["runtime_mode_usage"] == {}
    assert data["legacy_tool_loop_usage"] == {}
    assert data["orchestration_runtime_counts"] == {}
    assert data["active_legacy_task_count"] == 0
    assert data["active_unknown_task_count"] == 0
    assert data["avg_duration_seconds"] == 0.0
    assert data["success_rate"] == 0.0
    assert data["execution_health"]["outbox"] == {
        "pending_count": 0,
        "retrying_count": 0,
        "dead_letter_count": 0,
        "oldest_unresolved_age_seconds": None,
        "oldest_eligible_age_seconds": None,
        "affected_task_ids": [],
        "affected_task_ids_truncated": False,
    }
    assert data["execution_health"]["workers"]["fresh_count"] == 0
    assert data["execution_health"]["interactions"]["stuck_count"] == 0
    assert data["execution_health"]["reconciliation"]["status"] == "ok"
    assert data["execution_health"]["degraded_reasons"] == [
        "worker_unavailable",
        "dispatcher_unavailable",
    ]


def _create_health_signal_tasks(session, now):
    task_repo = TaskRepository(session)
    pending = task_repo.create(
        session_id="health",
        task_text="pending command",
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
    )
    retrying = task_repo.create(session_id="health", task_text="retrying command")
    dead = task_repo.create(session_id="health", task_text="dead command")
    terminal = task_repo.create(
        session_id="health",
        task_text="terminal projection",
        status=TaskStatus.COMPLETED,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
    )
    terminal.updated_at = now - timedelta(seconds=120)
    return pending, retrying, dead, terminal


def _seed_command_health(session, now, pending, retrying, dead, terminal) -> None:
    for task, attempts in ((pending, 0), (retrying, 2), (dead, 3)):
        TemporalCommandRepository(session).enqueue(
            task_id=task.id,
            command_type="start",
            command_key=f"start:{task.id}",
            payload={},
        )
        command = session.scalar(select(TemporalCommand).where(TemporalCommand.task_id == task.id))
        assert command is not None
        command.attempts = attempts
        command.created_at = now - timedelta(seconds=120)
        command.next_attempt_at = (
            now + timedelta(minutes=1) if task is retrying else now - timedelta(seconds=120)
        )
        if task is dead:
            command.dead_lettered_at = now - timedelta(seconds=30)

    for task in (pending, terminal):
        command = session.scalar(select(TemporalCommand).where(TemporalCommand.task_id == task.id))
        if command is None:
            TemporalCommandRepository(session).enqueue(
                task_id=task.id,
                command_type="start",
                command_key=f"start:{task.id}",
                payload={},
            )
            command = session.scalar(
                select(TemporalCommand).where(TemporalCommand.task_id == task.id)
            )
        assert command is not None
        command.delivered_at = now - timedelta(seconds=120)


def _seed_worker_and_interaction_health(session, now, pending) -> None:
    WorkerNodeRepository(session).register_worker(
        worker_id="fresh",
        worker_type="codex",
        now=now,
        capabilities={"command_dispatcher": True},
    )
    WorkerNodeRepository(session).register_worker(
        worker_id="stale",
        worker_type="codex",
        now=now - timedelta(seconds=31),
        capabilities={"command_dispatcher": True},
    )
    session.add_all(
        [
            HumanInteraction(
                task_id=pending.id,
                interaction_type=HumanInteractionType.CLARIFICATION,
                status=HumanInteractionStatus.PENDING,
                hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                summary="waiting",
                created_at=now - timedelta(days=2),
            ),
            HumanInteraction(
                task_id=pending.id,
                interaction_type=HumanInteractionType.PERMISSION,
                status=HumanInteractionStatus.PENDING,
                hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
                summary="recent",
                created_at=now,
            ),
        ]
    )


def test_get_metrics_reports_execution_health_signals(client, session_factory) -> None:
    """Metrics expose outbox, heartbeat, interaction, and reconciliation anomalies."""
    now = utc_now()
    with session_scope(session_factory) as session:
        pending, retrying, dead, terminal = _create_health_signal_tasks(session, now)
        _seed_command_health(session, now, pending, retrying, dead, terminal)
        _seed_worker_and_interaction_health(session, now, pending)

    probe = client.app.state.test_temporal_probe
    probe.workflow_statuses = {
        pending.id: "completed",
        terminal.id: "running",
        "missing-postgres-task": "running",
    }
    response = client.get("/metrics")

    assert response.status_code == http_status.HTTP_200_OK
    health = response.json()["execution_health"]
    assert health["outbox"]["pending_count"] == 0
    assert health["outbox"]["retrying_count"] == 1
    assert health["outbox"]["dead_letter_count"] == 1
    assert health["outbox"]["oldest_unresolved_age_seconds"] >= 119
    assert health["workers"]["fresh_count"] == 1
    assert health["workers"]["stale_count"] == 1
    assert health["workers"]["fresh_dispatcher_count"] == 1
    assert health["workers"]["freshest_dispatcher_heartbeat_at"] is not None
    assert health["workers"]["freshest_dispatcher_heartbeat_age_seconds"] < 1
    assert health["interactions"]["pending_count"] == 2
    assert health["interactions"]["stuck_count"] == 1
    assert health["interactions"]["affected_task_ids"] == [pending.id]
    assert health["reconciliation"]["status"] == "degraded"
    assert health["reconciliation"]["divergence_count"] == 3
    assert set(health["reconciliation"]["affected_task_ids"]) == {
        pending.id,
        terminal.id,
        "missing-postgres-task",
    }
    assert health["degraded_reasons"] == [
        "command_retries_present",
        "command_dead_letters_present",
        "interaction_wait_stuck",
        "terminal_state_divergence",
    ]


def test_get_metrics_reports_unknown_reconciliation_when_temporal_is_down(
    client: TestClient,
) -> None:
    """A Temporal outage must not be rendered as a healthy zero-divergence state."""
    client.app.state.test_temporal_probe.available = False

    response = client.get("/metrics")

    reconciliation = response.json()["execution_health"]["reconciliation"]
    assert reconciliation["status"] == "unknown"
    assert reconciliation["divergence_count"] is None
    assert (
        "terminal_reconciliation_unknown" in response.json()["execution_health"]["degraded_reasons"]
    )


def test_get_metrics_reports_legacy_submissions_since_cutover(
    client, session_factory, monkeypatch
) -> None:
    """The immutable deployment timestamp bounds the legacy retirement metric."""
    cutover_at = datetime(2026, 7, 18, 12, tzinfo=UTC)
    monkeypatch.setenv("TEMPORAL_ONLY_CUTOVER_AT", "2026-07-18T12:00:00Z")
    initialize_persisted_cutover(session_factory)
    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        before = task_repo.create(
            session_id="before",
            task_text="before",
            orchestration_runtime=OrchestrationRuntime.LEGACY,
        )
        after = task_repo.create(
            session_id="after", task_text="after", orchestration_runtime=OrchestrationRuntime.LEGACY
        )
        before.created_at = cutover_at - timedelta(seconds=1)
        after.created_at = cutover_at + timedelta(seconds=1)

    data = client.get("/metrics").json()

    assert data["temporal_only_cutover_at"] == "2026-07-18T12:00:00Z"
    assert data["legacy_submissions_since_cutover"] == 1


def test_get_metrics_with_windowing(client: TestClient, session_factory) -> None:
    """Metrics should be filterable by time window."""
    now = utc_now()

    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)

        # Recent task (within 24h)
        task_repo.create(session_id="s1", task_text="recent", status=TaskStatus.COMPLETED)

        # Old task (outside 24h)
        old_task = task_repo.create(session_id="s1", task_text="old", status=TaskStatus.COMPLETED)
        # Note: We have to manually set created_at because it's usually auto-filled
        old_task.created_at = now - timedelta(days=2)
        session.flush()

    # Default (24h) - should only see the recent one
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.json()["total_tasks"] == 1

    # Custom window (72h) - should see both
    resp = client.get("/metrics?window_hours=72")
    assert resp.status_code == 200
    assert resp.json()["total_tasks"] == 2

    # Disabled window (window_hours=0) - should see both
    resp = client.get("/metrics?window_hours=0")
    assert resp.status_code == 200
    assert resp.json()["total_tasks"] == 2
