import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TaskStatus
from db.models import TemporalCommand
from orchestrator.execution import TaskExecutionService
from repositories import (
    TaskRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


@pytest.fixture
def session_factory():
    """Create a SQLite-backed session factory for task endpoint tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


class SlowWorker(Worker):
    """Worker that sleeps to simulate long-running tasks."""

    def __init__(self, result: WorkerResult, delay: float = 1.0) -> None:
        self.result = result
        self.delay = delay
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
            return self.result
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.fixture
def slow_worker():
    return SlowWorker(
        WorkerResult(
            status="success",
            summary="Completed slowly",
            commands_run=[],
            files_changed=[],
            artifacts=[],
        ),
        delay=2.0,
    )


@pytest.fixture
def client(session_factory, slow_worker) -> TestClient:
    from apps.api.auth import ApiAuthConfig
    from apps.api.main import create_app

    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=slow_worker,
        ),
        auth_config=ApiAuthConfig(shared_secret=("a" * 32)),  # gitleaks:allow
    )
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = (
            "a" * 32  # gitleaks:allow
        )
        yield test_client


def test_cancel_temporal_task_queues_workflow_cancellation(
    client: TestClient,
):
    """Temporal-backed cancellation should enqueue a durable cancel command."""
    response = client.post("/tasks", json={"task_text": "Temporal cancellation"})
    task_id = response.json()["task_id"]

    cancel_response = client.post(f"/tasks/{task_id}/cancel")

    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "failed"
    with session_scope(client.app.state.task_service.session_factory) as session:
        command = session.query(TemporalCommand).filter_by(command_type="cancel").one()
        assert command.task_id == task_id


def test_cancel_terminal_task_is_ignored(client: TestClient, session_factory):
    """Cancelling an already terminal task should return the task unchanged."""
    response = client.post("/tasks", json={"task_text": "Terminal test"})
    task_id = response.json()["task_id"]

    # Mark as completed manually
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_id)
        task.status = TaskStatus.COMPLETED
        session.flush()

    cancel_response = client.post(f"/tasks/{task_id}/cancel")
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "completed"


def test_cancelled_task_is_terminal(client: TestClient, session_factory):
    """Once cancelled, a task cannot transition back to pending or in_progress."""
    response = client.post("/tasks", json={"task_text": "Terminal test"})
    task_id = response.json()["task_id"]

    client.post(f"/tasks/{task_id}/cancel")

    # Try to approve it if it was waiting (it shouldn't be, but let's test the state machine)
    # Re-using the conflict logic in apply_task_approval_decision
    approve_response = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert approve_response.status_code == 409
