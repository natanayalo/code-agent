import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.auth import API_SHARED_SECRET_HEADER, ApiAuthConfig
from apps.api.main import create_app
from db.base import Base
from db.enums import HumanInteractionStatus, HumanInteractionType
from orchestrator.brain import OrchestratorBrain, TaskSpecBrainSuggestion
from orchestrator.execution import TaskExecutionService
from repositories import (
    HumanInteractionRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


class MockBrain(OrchestratorBrain):
    def __init__(self):
        self.suggestion = TaskSpecBrainSuggestion()

    async def suggest_task_spec(self, **kwargs) -> TaskSpecBrainSuggestion | None:
        return self.suggestion

    async def suggest_route(self, **kwargs):
        return None

    async def suggest_verification(self, **kwargs):
        return None


class StaticWorker(Worker):
    async def run(self, request: WorkerRequest, **kwargs) -> WorkerResult:
        return WorkerResult(status="success", summary="done")


@pytest.fixture
def session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def brain():
    return MockBrain()


@pytest.fixture
def client(session_factory, brain) -> TestClient:
    service = TaskExecutionService(
        session_factory=session_factory,
        worker=StaticWorker(),
        orchestrator_brain=brain,
        checkpoint_path=":memory:",  # Use in-memory sqlite for checkpoints too
    )
    app = create_app(task_service=service, auth_config=ApiAuthConfig(shared_secret="test-secret"))
    # Ensure lifespan runs so app.state is populated
    with TestClient(app) as test_client:
        yield test_client


def test_clarification_workflow(client: TestClient, brain: MockBrain, session_factory):
    headers = {API_SHARED_SECRET_HEADER: "test-secret"}

    # 1. Trigger clarification requirement
    brain.suggestion = TaskSpecBrainSuggestion(clarification_questions=["Which color?"])

    resp = client.post("/tasks", json={"task_text": "paint it"}, headers=headers)
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]

    # Run orchestrator
    service = client.app.state.task_service
    asyncio.run(service.run_queued_task(task_id=task_id, worker_id="test"))

    # Verify task is PENDING (paused) and has interaction
    resp = client.get(f"/tasks/{task_id}", headers=headers)
    assert resp.json()["status"] == "pending"

    with session_scope(session_factory) as session:
        interactions = HumanInteractionRepository(session).list_by_task(task_id=task_id)
        assert len(interactions) == 1
        interaction = interactions[0]
        assert interaction.interaction_type == HumanInteractionType.CLARIFICATION
        assert interaction.status == HumanInteractionStatus.PENDING
        interaction_id = interaction.id

    # 2. Resolve interaction
    resp = client.post(
        f"/tasks/{task_id}/interactions/{interaction_id}/response",
        json={"response_data": {"answer": "blue"}},
        headers=headers,
    )
    assert resp.status_code == 200

    # Run orchestrator again
    asyncio.run(service.run_queued_task(task_id=task_id, worker_id="test"))

    # Verify task completed
    resp = client.get(f"/tasks/{task_id}", headers=headers)
    assert resp.json()["status"] == "completed"


def test_permission_workflow_skips_approval(client: TestClient, brain: MockBrain, session_factory):
    headers = {API_SHARED_SECRET_HEADER: "test-secret"}

    # 1. Trigger permission requirement
    brain.suggestion = TaskSpecBrainSuggestion(suggested_risk_level="high")

    resp = client.post("/tasks", json={"task_text": "delete all"}, headers=headers)
    task_id = resp.json()["task_id"]

    service = client.app.state.task_service
    asyncio.run(service.run_queued_task(task_id=task_id, worker_id="test"))

    # Verify paused for permission
    with session_scope(session_factory) as session:
        interactions = HumanInteractionRepository(session).list_by_task(task_id=task_id)
        assert len(interactions) == 1
        interaction = interactions[0]
        assert interaction.interaction_type == HumanInteractionType.PERMISSION
        interaction_id = interaction.id

    # 2. Resolve permission
    client.post(
        f"/tasks/{task_id}/interactions/{interaction_id}/response",
        json={"response_data": {"granted": True}},
        headers=headers,
    )

    # Run orchestrator again
    asyncio.run(service.run_queued_task(task_id=task_id, worker_id="test"))

    # Verify task completed (it should HAVE skipped check_approval because we satisfied it)
    resp = client.get(f"/tasks/{task_id}", headers=headers)
    assert resp.json()["status"] == "completed"


def test_content_change_re_pauses(client: TestClient, brain: MockBrain, session_factory):
    headers = {API_SHARED_SECRET_HEADER: "test-secret"}

    # 1. Resolve one clarification
    brain.suggestion = TaskSpecBrainSuggestion(clarification_questions=["What color?"])
    resp = client.post("/tasks", json={"task_text": "paint"}, headers=headers)
    task_id = resp.json()["task_id"]

    service = client.app.state.task_service
    asyncio.run(service.run_queued_task(task_id=task_id, worker_id="test"))

    with session_scope(session_factory) as session:
        interaction = HumanInteractionRepository(session).list_by_task(task_id=task_id)[0]
        interaction_id = interaction.id

    client.post(
        f"/tasks/{task_id}/interactions/{interaction_id}/response",
        json={"response_data": {"answer": "red"}},
        headers=headers,
    )

    # 2. Now change the brain to ask a DIFFERENT question for the same task
    # (simulating a retry or change in logic)
    brain.suggestion = TaskSpecBrainSuggestion(
        clarification_questions=["What shade of red?"]  # Content changed!
    )

    # Run orchestrator
    asyncio.run(service.run_queued_task(task_id=task_id, worker_id="test"))

    # Verify it PAUSED AGAIN because the hash mismatched
    resp = client.get(f"/tasks/{task_id}", headers=headers)
    assert resp.json()["status"] == "pending"

    with session_scope(session_factory) as session:
        interactions = HumanInteractionRepository(session).list_by_task(task_id=task_id)
        # Should have 2 interactions now (one resolved, one pending)
        pending = [i for i in interactions if i.status == HumanInteractionStatus.PENDING]
        assert len(pending) == 1
        assert pending[0].data["questions"] == ["What shade of red?"]
