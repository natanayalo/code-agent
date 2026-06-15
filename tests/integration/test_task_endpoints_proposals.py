"""Integration tests for proposal API endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.enums import ProposalStatus
from orchestrator.execution import ProposalSnapshot, TaskExecutionService


class MockTaskExecutionService(TaskExecutionService):
    def __init__(self):
        # We don't need a real session factory or worker for this mock
        pass

    def list_proposals(
        self,
        *,
        status: ProposalStatus | str | None = None,
        proposal_type: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ProposalSnapshot]:
        return [
            ProposalSnapshot(
                proposal_id="00000000-0000-0000-0000-000000000000",
                session_id="00000000-0000-0000-0000-000000000000",
                title="Mock Proposal",
                summary="Mock summary",
                status=str(status) if status else "pending_review",
                proposal_type=proposal_type or "scout",
                metadata_payload={},
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        ]


@pytest.fixture
def client() -> TestClient:
    app = create_app(
        task_service=MockTaskExecutionService(),
        auth_config=ApiAuthConfig(shared_secret=("a" * 32)),
    )
    with TestClient(app) as test_client:
        test_client.headers["X-Webhook-Token"] = "a" * 32
        yield test_client


def test_list_proposals_without_filters(client: TestClient) -> None:
    response = client.get("/proposals")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["proposal_type"] == "scout"


def test_list_proposals_with_proposal_type_filter(client: TestClient) -> None:
    response = client.get("/proposals?proposal_type=reflection")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["proposal_type"] == "reflection"


def test_list_proposals_with_invalid_proposal_type(client: TestClient) -> None:
    response = client.get("/proposals?proposal_type=invalid_type")
    assert response.status_code == 422  # FastAPI validation error
