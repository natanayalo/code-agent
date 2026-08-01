"""Integration tests for process health and execution readiness."""

from __future__ import annotations

from datetime import timedelta

from fastapi import status
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import app, create_app
from db.base import utc_now
from db.enums import (
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
)
from db.models import HumanInteraction
from orchestrator.execution import TaskExecutionService, TaskSubmission
from repositories import (
    TaskRepository,
    TemporalCommandRepository,
    WorkerNodeRepository,
    session_scope,
)
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET, _default_worker


class MutableTemporalProbe:
    """Temporal probe double that can recover without rebuilding the app."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.workflow_statuses: dict[str, str] = {}

    def is_available(self) -> bool:
        return self.available

    def list_task_workflow_statuses(self) -> dict[str, str]:
        if not self.available:
            raise ConnectionError("Temporal unavailable")
        return dict(self.workflow_statuses)


def _configured_client(session_factory, probe: MutableTemporalProbe) -> TestClient:
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=_default_worker(),
        temporal_operational_probe=probe,
    )
    configured_app = create_app(
        task_service=service,
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )
    return TestClient(configured_app)


def _register_worker(session_factory, *, heartbeat_offset_seconds: int = 0) -> None:
    now = utc_now()
    with session_scope(session_factory) as session:
        WorkerNodeRepository(session).register_worker(
            worker_id="temporal-worker",
            worker_type="codex",
            now=now - timedelta(seconds=heartbeat_offset_seconds),
            capabilities={"command_dispatcher": True},
        )


def test_health_remains_process_liveness() -> None:
    """Health must stay independent from execution dependencies."""
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


def test_ready_returns_structured_unconfigured_state() -> None:
    """An API process without task execution is alive but not execution-ready."""
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["degraded_reasons"] == ["task_service_unconfigured"]
    assert list(payload["components"]) == ["postgres", "temporal", "worker", "dispatcher"]
    assert all(
        component
        == {
            "status": "unknown",
            "reasons": ["task_service_unconfigured"],
            "last_observed_at": None,
        }
        for component in payload["components"].values()
    )


def test_ready_reports_all_dependencies_ready(session_factory) -> None:
    """Fresh worker/dispatcher evidence plus healthy dependencies returns 200."""
    _register_worker(session_factory)
    with _configured_client(session_factory, MutableTemporalProbe()) as client:
        response = client.get("/ready")

    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["degraded_reasons"] == []
    assert all(component["status"] == "ready" for component in payload["components"].values())


def test_ready_recovers_from_temporal_outage_without_api_restart(session_factory) -> None:
    """Temporal recovery is observed on the next probe in the same process."""
    _register_worker(session_factory)
    probe = MutableTemporalProbe(available=False)
    with _configured_client(session_factory, probe) as client:
        unavailable = client.get("/ready")
        probe.available = True
        recovered = client.get("/ready")

    assert unavailable.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert unavailable.json()["components"]["temporal"] == {
        "status": "not_ready",
        "reasons": ["temporal_unavailable"],
        "last_observed_at": None,
    }
    assert recovered.status_code == status.HTTP_200_OK


def test_ready_reports_missing_and_stale_worker(session_factory) -> None:
    """Missing or expired worker evidence blocks execution readiness."""
    probe = MutableTemporalProbe()
    with _configured_client(session_factory, probe) as client:
        missing = client.get("/ready")
        _register_worker(session_factory, heartbeat_offset_seconds=31)
        stale = client.get("/ready")

    for response in (missing, stale):
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        payload = response.json()
        assert payload["components"]["worker"]["reasons"] == ["worker_unavailable"]
        assert payload["components"]["dispatcher"]["reasons"] == ["dispatcher_unavailable"]


def test_ready_reports_stale_deliverable_outbox(session_factory) -> None:
    """A fresh dispatcher cannot hide a command that has been eligible for too long."""
    _register_worker(session_factory)
    now = utc_now()
    with session_scope(session_factory) as session:
        task = TaskRepository(session).create(session_id="s1", task_text="queued")
        TemporalCommandRepository(session).enqueue(
            task_id=task.id,
            command_type="start",
            command_key=f"start:{task.id}",
            payload={},
        )
        command = task.temporal_commands[0]
        command.created_at = now - timedelta(seconds=61)
        command.next_attempt_at = now - timedelta(seconds=61)

    with _configured_client(session_factory, MutableTemporalProbe()) as client:
        response = client.get("/ready")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["components"]["dispatcher"]["reasons"] == ["dispatcher_backlog_stale"]


def test_ready_recovers_from_database_outage_without_api_restart(session_factory) -> None:
    """The same API process observes database restoration on its next readiness probe."""
    _register_worker(session_factory)

    class SwitchableSessionFactory:
        available = False

        def __call__(self):
            if not self.available:
                raise RuntimeError("database unavailable")
            return session_factory()

    switchable = SwitchableSessionFactory()
    service = TaskExecutionService(
        session_factory=switchable,  # type: ignore[arg-type]
        worker=_default_worker(),
        temporal_operational_probe=MutableTemporalProbe(),
    )
    configured_app = create_app(
        task_service=service,
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )
    with TestClient(configured_app) as client:
        unavailable = client.get("/ready")
        switchable.available = True
        recovered = client.get("/ready")

    assert unavailable.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert unavailable.json()["components"]["postgres"]["reasons"] == ["postgres_unavailable"]
    assert recovered.status_code == status.HTTP_200_OK


def test_temporal_outage_preserves_reads_and_interaction_responses(session_factory) -> None:
    """Temporal degradation blocks submissions without blocking durable operator actions."""
    probe = MutableTemporalProbe()
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=_default_worker(),
        enforce_temporal_availability=True,
        temporal_operational_probe=probe,
    )
    snapshot, _ = service.create_task(TaskSubmission(task_text="Await clarification"))
    with session_scope(session_factory) as session:
        interaction = HumanInteraction(
            task_id=snapshot.task_id,
            interaction_type=HumanInteractionType.CLARIFICATION,
            status=HumanInteractionStatus.PENDING,
            hitl_mode=HumanInteractionHitlMode.REQUIRE_APPROVAL,
            summary="Need an answer",
        )
        session.add(interaction)
        session.flush()
        interaction_id = interaction.id

    configured_app = create_app(
        task_service=service,
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )
    probe.available = False
    with TestClient(configured_app) as client:
        client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET
        task_read = client.get(f"/tasks/{snapshot.task_id}")
        interaction_response = client.post(
            f"/tasks/{snapshot.task_id}/interactions/{interaction_id}/response",
            json={"response_data": {"answer": "Proceed"}},
        )
        submission = client.post("/tasks", json={"task_text": "Blocked submission"})

    assert task_read.status_code == status.HTTP_200_OK
    assert interaction_response.status_code == status.HTTP_200_OK
    assert interaction_response.json()["pending_interactions"] == []
    assert submission.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
