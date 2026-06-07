"""Integration tests for task replay endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from repositories import TaskRepository, session_scope
from tests.integration.task_endpoints_support import _run_one_queued_task


def test_task_replay_endpoint_creates_replayable_task_with_provenance(
    client: TestClient,
    session_factory,
) -> None:
    """Replaying a terminal task should create a fresh queued task with audit provenance."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Create a note and report the result",
            "constraints": {"assumptions": ["original run"]},
            "budget": {"max_iterations": 3},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user-replay",
                "external_thread_id": "thread-replay",
            },
        },
    )
    assert response.status_code == 202
    source_task_id = response.json()["task_id"]

    _run_one_queued_task(client)

    replay_response = client.post(
        f"/tasks/{source_task_id}/replay",
        json={
            "constraints": {"assumptions": ["second pass"]},
            "budget": {"max_iterations": 6},
        },
    )

    assert replay_response.status_code == 201
    replay_payload = replay_response.json()
    replay_task_id = replay_payload["task_id"]

    assert replay_task_id != source_task_id
    assert replay_payload["status"] == "pending"
    assert replay_payload["latest_run"] is None

    with session_scope(session_factory) as session:
        source_task = TaskRepository(session).get(source_task_id)
        replayed_task = TaskRepository(session).get(replay_task_id)

        assert source_task is not None
        assert replayed_task is not None
        assert replayed_task.task_text == source_task.task_text
        assert replayed_task.constraints["replayed_from"] == [source_task_id]
        assert replayed_task.constraints["assumptions"] == ["second pass"]
        assert replayed_task.budget["max_iterations"] == 6

    _run_one_queued_task(client)

    completed = client.get(f"/tasks/{replay_task_id}")
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    worker = client.app.state.test_worker
    assert len(worker.requests) == 2


def test_task_replay_endpoint_rejects_non_terminal_source_task(
    client: TestClient,
    session_factory,
) -> None:
    """Replay should fail closed when the source task has not reached a terminal state."""
    response = client.post(
        "/tasks",
        json={
            "task_text": "Create a note and report the result",
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user-replay-pending",
                "external_thread_id": "thread-replay-pending",
            },
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    replay_response = client.post(f"/tasks/{task_id}/replay", json={})

    assert replay_response.status_code == 409
    assert "cannot be replayed" in replay_response.json()["detail"].lower()

    with session_scope(session_factory) as session:
        listed = TaskRepository(session).list_by_session(response.json()["session_id"])
        assert [task.id for task in listed] == [task_id]
