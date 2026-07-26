"""Integration tests for approval and interaction task endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from repositories import TaskRepository, session_scope
from tests.integration.task_endpoints_support import _run_one_temporal_task


def test_task_approval_endpoint_rejects_tasks_not_waiting_for_decision(client: TestClient) -> None:
    """Approval endpoint should fail for tasks that are not in a paused-approval state."""
    response = client.post("/tasks", json={"task_text": "Create a README section"})
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    not_waiting_response = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert not_waiting_response.status_code == 409
    assert "not currently awaiting" in not_waiting_response.json()["detail"].lower()


def test_interaction_response_endpoint_requeues_permission_gated_task(
    client: TestClient,
    session_factory,
) -> None:
    """Resolving a permission interaction should clear the gate and resume the queued task."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user-interaction",
                "external_thread_id": "thread-interaction",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    pending = response.json()["pending_interactions"]
    assert len(pending) == 1

    interaction_id = pending[0]["interaction_id"]
    interaction_response = client.post(
        f"/tasks/{task_id}/interactions/{interaction_id}/response",
        json={"response_data": {"approved": True}},
    )

    assert interaction_response.status_code == 200
    interaction_payload = interaction_response.json()
    assert interaction_payload["status"] == "pending"
    assert interaction_payload["pending_interaction_count"] == 0
    assert interaction_payload["pending_interactions"] == []
    assert interaction_payload["latest_run"] is None
    assert any(
        event["event_type"] == "approval_granted" for event in interaction_payload["timeline"]
    )

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.constraints["requires_approval"] is False
        assert task.constraints["approval"]["status"] == "approved"

    _run_one_temporal_task(client)

    resumed = client.get(f"/tasks/{task_id}")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
