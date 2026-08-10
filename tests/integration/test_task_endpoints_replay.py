"""Integration tests for task replay endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from repositories import TaskRepository, session_scope
from tests.integration.task_endpoints_support import _run_one_temporal_task
from workers import WorkerResult


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

    _run_one_temporal_task(client)

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

    _run_one_temporal_task(client)

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


def test_task_replay_carries_compact_session_state_and_clears_stale_failure(
    client: TestClient,
) -> None:
    """A replay should receive typed prior context and replace resolved failure details."""
    worker = client.app.state.test_worker
    worker.result = WorkerResult(
        status="failure",
        summary="The focused test failed.",
        failure_kind="test",
        requested_permission="workspace_write",
        files_changed=["first_attempt.py"],
    )
    response = client.post(
        "/tasks",
        json={
            "task_text": "Repair the session-state regression",
            "constraints": {"skip_independent_review": True},
            "session": {
                "channel": "http",
                "external_user_id": "http:test-user-session-continuity",
                "external_thread_id": "thread-session-continuity",
            },
        },
    )
    assert response.status_code == 202
    source_task_id = response.json()["task_id"]
    session_id = response.json()["session_id"]

    _run_one_temporal_task(client)

    failed = client.get(f"/tasks/{source_task_id}")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    failed_context = client.get(f"/sessions/{session_id}")
    assert failed_context.status_code == 200
    prior_risks = failed_context.json()["working_context"]["identified_risks"]
    assert prior_risks["worker_status"] == "failure"
    assert prior_risks["worker_failure_kind"] is not None
    requests_before_replay = len(worker.requests)

    worker.result = WorkerResult(
        status="success",
        summary="The focused test now passes.",
        files_changed=["second_attempt.py"],
    )
    replay = client.post(f"/tasks/{source_task_id}/replay", json={})
    assert replay.status_code == 201

    _run_one_temporal_task(client)

    assert len(worker.requests) == requests_before_replay + 1
    resumed_context = worker.requests[-1].memory_context["session"]
    assert (
        resumed_context["identified_risks"]["worker_failure_kind"]
        == prior_risks["worker_failure_kind"]
    )
    assert (
        resumed_context["identified_risks"]["requested_permission"]
        == prior_risks["requested_permission"]
    )
    assert resumed_context["files_touched"] == ["first_attempt.py"]

    updated_context = client.get(f"/sessions/{session_id}")
    assert updated_context.status_code == 200
    working_context = updated_context.json()["working_context"]
    assert working_context["decisions_made"]["task_type"] == "bugfix"
    assert working_context["identified_risks"]["worker_status"] == "success"
    assert working_context["identified_risks"]["worker_failure_kind"] is None
    assert working_context["identified_risks"]["requested_permission"] is None
    assert working_context["files_touched"] == ["first_attempt.py", "second_attempt.py"]
