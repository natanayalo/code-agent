import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from db.base import Base
from db.enums import TaskStatus, TimelineEventType
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
        auth_config=ApiAuthConfig(shared_secret="test-secret"),
    )
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = "test-secret"
        yield test_client


def test_cancel_pending_task_prevents_claim(client: TestClient, session_factory):
    """A cancelled task should never be picked up by a worker."""
    response = client.post("/tasks", json={"task_text": "Pending task"})
    task_id = response.json()["task_id"]

    # Cancel it immediately
    cancel_response = client.post(f"/tasks/{task_id}/cancel")
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "failed"

    # Try to claim it
    service = client.app.state.task_service
    claim = service.claim_next_task(worker_id="test-worker", lease_seconds=60)
    assert claim is None


def test_cancel_in_progress_task_aborts_execution(client: TestClient, session_factory, slow_worker):
    """Cancelling an in-progress task should stop the heartbeat and abort the orchestrator."""
    response = client.post("/tasks", json={"task_text": "Long task"})
    task_id = response.json()["task_id"]

    service = client.app.state.task_service
    claim = service.claim_next_task(worker_id="test-worker", lease_seconds=3)
    assert claim is not None

    async def run_task():
        return await service.run_queued_task(
            task_id=task_id, worker_id="test-worker", lease_seconds=3
        )

    async def test_flow():
        # Start execution
        exec_task = asyncio.create_task(run_task())

        # Wait for worker to start
        await asyncio.wait_for(slow_worker.started.wait(), timeout=5.0)

        # Cancel the task via API
        cancel_response = client.post(f"/tasks/{task_id}/cancel")
        assert cancel_response.status_code == 200

        # Wait for execution to be aborted by heartbeat (lease is 3s, interval is 1s)
        await asyncio.wait_for(exec_task, timeout=10.0)

        return exec_task.result()

    asyncio.run(test_flow())

    assert slow_worker.cancelled is True

    # Verify final status
    get_response = client.get(f"/tasks/{task_id}")
    assert get_response.json()["status"] == "failed"
    assert "cancelled" in get_response.json()["last_error"].lower()

    # Verify timeline
    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_id)
        events = [e.event_type for e in task.timeline_events]
        assert TimelineEventType.TASK_CANCELLED in events


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

    # Attempt to "re-queue" it by manually setting status in DB
    # (if we could, but repo should block it)
    # Actually, we should check if claim_next_task ignores it.
    service = client.app.state.task_service
    claim = service.claim_next_task(worker_id="test-worker", lease_seconds=60)
    assert claim is None

    # Try to approve it if it was waiting (it shouldn't be, but let's test the state machine)
    # Re-using the conflict logic in apply_task_approval_decision
    approve_response = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert approve_response.status_code == 409
