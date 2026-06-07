"""Integration tests for approval and interaction task endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from repositories import TaskRepository, session_scope
from tests.integration.task_endpoints_support import _run_one_queued_task


def test_queued_task_requires_approval_before_worker_dispatch(client: TestClient) -> None:
    """requires_approval should halt execution before the worker is invoked."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"requires_approval": True, "approval_reason": "Manual safety gate"},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    get_response = client.get(f"/tasks/{task_id}")
    assert get_response.status_code == 200
    payload = get_response.json()
    assert payload["status"] == "pending"
    assert payload["latest_run"] is not None
    assert payload["latest_run"]["status"] == "failure"
    assert "approval" in payload["latest_run"]["summary"].lower()

    worker = client.app.state.test_worker
    assert len(worker.requests) == 0


def test_queued_task_ignores_injected_approval_constraints(client: TestClient) -> None:
    """User-supplied approval status must not bypass destructive-task approval gates."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"approval": {"status": "approved", "source": "api"}},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval-spoof",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    get_response = client.get(f"/tasks/{task_id}")
    assert get_response.status_code == 200
    payload = get_response.json()
    assert payload["status"] == "pending"
    assert payload["latest_run"] is not None
    assert payload["latest_run"]["status"] == "failure"
    assert "approval" in payload["latest_run"]["summary"].lower()

    worker = client.app.state.test_worker
    assert len(worker.requests) == 0

    with session_scope(client.app.state.task_service.session_factory) as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert isinstance(task.constraints, dict)
        approval = task.constraints.get("approval")
        assert isinstance(approval, dict)
        assert approval.get("source") == "orchestrator"


def test_task_approval_endpoint_requeues_approved_task(client: TestClient) -> None:
    """Approving a paused task should requeue it for the next worker claim."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"requires_approval": True, "approval_reason": "Manual safety gate"},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval-resume",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    paused = client.get(f"/tasks/{task_id}")
    assert paused.status_code == 200
    assert paused.json()["status"] == "pending"
    assert "approval" in paused.json()["latest_run"]["summary"].lower()

    approve_response = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "pending"

    duplicate_approve = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert duplicate_approve.status_code == 200
    assert duplicate_approve.json()["status"] == "pending"

    _run_one_queued_task(client)
    resumed = client.get(f"/tasks/{task_id}")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"

    worker = client.app.state.test_worker
    assert len(worker.requests) == 1


def test_task_approval_endpoint_rejects_task_terminally(client: TestClient) -> None:
    """Rejecting a paused task should keep it terminal with a clear decision summary."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"requires_approval": True},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval-reject",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    reject_response = client.post(f"/tasks/{task_id}/approval", json={"approved": False})
    assert reject_response.status_code == 200
    payload = reject_response.json()
    assert payload["status"] == "failed"
    assert payload["latest_run"] is not None
    assert "rejected" in payload["latest_run"]["summary"].lower()

    duplicate_reject = client.post(f"/tasks/{task_id}/approval", json={"approved": False})
    assert duplicate_reject.status_code == 200
    assert duplicate_reject.json()["status"] == "failed"

    worker = client.app.state.test_worker
    assert len(worker.requests) == 0


def test_task_approval_endpoint_rejects_conflicting_duplicate_decisions(client: TestClient) -> None:
    """Once approved/rejected, the opposite decision should return a conflict."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Delete all local files",
            "constraints": {"requires_approval": True},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user",
                "external_thread_id": "thread-approval-conflict",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    _run_one_queued_task(client)
    approve_response = client.post(f"/tasks/{task_id}/approval", json={"approved": True})
    assert approve_response.status_code == 200

    conflict_response = client.post(f"/tasks/{task_id}/approval", json={"approved": False})
    assert conflict_response.status_code == 409
    assert "already recorded" in conflict_response.json()["detail"].lower()


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

    _run_one_queued_task(client)

    resumed = client.get(f"/tasks/{task_id}")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
