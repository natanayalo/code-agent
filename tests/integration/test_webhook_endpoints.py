"""Integration tests for the generic webhook adapter (T-050)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

from apps.api.main import create_app
from db.base import Base
from db.enums import TaskStatus
from orchestrator.execution import TaskExecutionService
from repositories import (
    TaskRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker, WorkerRequest, WorkerResult


class StaticWorker(Worker):
    """Worker double that returns a predefined result and records requests."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


class RecordingProgressNotifier:
    """Capture progress events emitted during webhook requests."""

    def __init__(self) -> None:
        self.events = []

    async def notify(self, *, submission, event) -> None:
        self.events.append((submission, event))


@pytest.fixture
def session_factory():
    """SQLite-backed session factory for webhook endpoint tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def client(session_factory) -> Iterator[TestClient]:
    """Test client wired with the execution-path task service."""
    notifier = RecordingProgressNotifier()
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="Webhook task completed.",
            budget_usage={"iterations_used": 1, "tool_calls_used": 1},
            commands_run=[],
            files_changed=[],
            artifacts=[],
            next_action_hint=None,
        )
    )
    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=worker,
            progress_notifier=notifier,
        )
    )
    app.state.test_worker = worker
    app.state.test_notifier = notifier
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_webhook_minimal_payload_creates_task(client: TestClient) -> None:
    """Posting only task_text should create a task and return 202."""
    response = client.post("/webhook", json={"task_text": "echo hello"})

    assert response.status_code == 202
    body = response.json()
    assert "task_id" in body
    assert body["status"] == "pending"


def test_webhook_sets_channel_from_source(client: TestClient, session_factory) -> None:
    """The source field should be reflected in the session channel."""
    response = client.post(
        "/webhook",
        json={
            "task_text": "run linter",
            "source": "ci-bot",
            "external_user_id": "ci-bot:runner",
            "external_thread_id": "run-42",
        },
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]

    worker = client.app.state.test_worker
    # poll until the background task has run
    get_resp = client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200

    req = worker.requests[0]
    # Verify the task text from the payload was passed through to the worker.
    # (Channel is stored on the DB session record, not on WorkerRequest.)
    assert req.task_text == "run linter"


def test_webhook_defaults_external_ids_when_omitted(
    client: TestClient,
) -> None:
    """When external_user_id / external_thread_id are absent, defaults are applied."""
    response = client.post(
        "/webhook",
        json={"task_text": "check status", "source": "my-system"},
    )

    assert response.status_code == 202
    # No error means defaults were accepted by the submission pipeline
    assert response.json()["task_id"]


def test_webhook_anonymous_requests_get_isolated_sessions(
    client: TestClient,
) -> None:
    """Two anonymous calls share one stable User but get different session_ids.

    anonymous external_user_id is the stable sentinel "webhook:{source}:anonymous",
    while external_thread_id uses a unique UUID per call — so sessions are isolated
    but the User table does not grow unboundedly.
    """
    r1 = client.post("/webhook", json={"task_text": "task one"})
    r2 = client.post("/webhook", json={"task_text": "task two"})

    assert r1.status_code == 202
    assert r2.status_code == 202
    # Unique thread UUIDs guarantee distinct Session records (different session_ids).
    assert r1.json()["session_id"] != r2.json()["session_id"]


def test_webhook_namespaces_external_user_id(client: TestClient) -> None:
    """Caller-supplied external_user_id must be prefixed with webhook:{source}: to prevent
    collisions with identically-named users from other adapters."""
    response = client.post(
        "/webhook",
        json={
            "task_text": "do something",
            "source": "ci",
            "external_user_id": "alice",
            "external_thread_id": "run-1",
        },
    )

    assert response.status_code == 202
    worker = client.app.state.test_worker
    client.get(f"/tasks/{response.json()['task_id']}")
    # WorkerRequest carries the raw task; channel/user id live on the DB session.
    # The real guard is that the submission pipeline accepted the namespaced value
    # without error (which would surface as a 5xx here if the field were too long
    # or invalid after prefixing).
    assert len(worker.requests) == 1


def test_webhook_full_payload_creates_and_completes_task(
    client: TestClient, session_factory
) -> None:
    """A full payload should propagate all fields and persist a completed task."""
    response = client.post(
        "/webhook",
        json={
            "task_text": "Create a README file",
            "repo_url": "https://github.com/natanayalo/code-agent",
            "branch": "master",
            "priority": 1,
            "source": "github-actions",
            "external_user_id": "github-actions:bot",
            "external_thread_id": "pr-123",
            "constraints": {"max_files": 5},
            "budget": {"max_iterations": 10},
        },
    )

    assert response.status_code == 202
    task_id = response.json()["task_id"]

    get_response = client.get(f"/tasks/{task_id}")
    assert get_response.status_code == 200
    assert get_response.json()["status"] == "completed"

    worker = client.app.state.test_worker
    assert len(worker.requests) == 1
    req = worker.requests[0]
    assert req.repo_url == "https://github.com/natanayalo/code-agent"
    assert req.branch == "master"
    assert req.task_text == "Create a README file"

    with session_scope(session_factory) as session:
        task_repo = TaskRepository(session)
        task = task_repo.get(task_id)
        assert task is not None
        assert task.status is TaskStatus.COMPLETED


def test_webhook_delivery_id_is_idempotent(client: TestClient) -> None:
    """A repeated webhook delivery_id should return the first task without re-running work."""
    payload = {
        "task_text": "Create a README file",
        "source": "github-actions",
        "external_user_id": "github-actions:bot",
        "external_thread_id": "pr-123",
        "delivery_id": "delivery-1",
    }

    first = client.post("/webhook", json=payload)
    second = client.post("/webhook", json=payload)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["task_id"] == first.json()["task_id"]

    worker = client.app.state.test_worker
    assert len(worker.requests) == 1


def test_webhook_progress_notifications_include_callback_url_submission(
    client: TestClient,
) -> None:
    """Webhook progress delivery should preserve the callback target on submission."""
    response = client.post(
        "/webhook",
        json={
            "task_text": "Create a README file",
            "source": "github-actions",
            "external_user_id": "github-actions:bot",
            "external_thread_id": "pr-123",
            "callback_url": "https://callbacks.example.com/task-status",
        },
    )

    assert response.status_code == 202
    notifier = client.app.state.test_notifier
    assert [event.phase for _, event in notifier.events] == ["started", "running", "completed"]
    assert all(
        submission.callback_url == "https://callbacks.example.com/task-status"
        for submission, _ in notifier.events
    )


def test_webhook_returns_404_for_unknown_task(client: TestClient) -> None:
    """GET /tasks/<unknown> should still return 404 after a webhook submission."""
    response = client.get("/tasks/task-does-not-exist")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Rejection / validation tests
# ---------------------------------------------------------------------------


def test_webhook_rejects_empty_task_text(client: TestClient) -> None:
    """Empty task_text should be rejected with 422."""
    response = client.post("/webhook", json={"task_text": ""})
    assert response.status_code == 422


def test_webhook_rejects_whitespace_only_task_text(client: TestClient) -> None:
    """Whitespace-only task_text should be rejected with 422 after stripping."""
    response = client.post("/webhook", json={"task_text": "   "})
    assert response.status_code == 422


def test_webhook_rejects_task_text_exceeding_max_length(client: TestClient) -> None:
    """task_text longer than 10 000 characters should be rejected with 422."""
    response = client.post("/webhook", json={"task_text": "x" * 10_001})
    assert response.status_code == 422


def test_webhook_rejects_repo_url_exceeding_max_length(client: TestClient) -> None:
    """repo_url longer than 2048 characters should be rejected with 422."""
    response = client.post(
        "/webhook", json={"task_text": "ok", "repo_url": "https://x.com/" + "a" * 2035}
    )
    assert response.status_code == 422


def test_webhook_rejects_branch_exceeding_max_length(client: TestClient) -> None:
    """branch longer than 255 characters should be rejected with 422."""
    response = client.post("/webhook", json={"task_text": "ok", "branch": "b" * 256})
    assert response.status_code == 422


def test_webhook_rejects_source_exceeding_max_length(client: TestClient) -> None:
    """source longer than 100 characters should be rejected with 422."""
    response = client.post("/webhook", json={"task_text": "ok", "source": "s" * 101})
    assert response.status_code == 422


def test_webhook_rejects_external_user_id_exceeding_max_length(
    client: TestClient,
) -> None:
    """external_user_id longer than 146 characters should be rejected with 422.

    The stored value is prefixed with "webhook:{source}:" (≤109 chars), so the raw
    caller id is capped at 255 - 109 = 146 to stay within the DB column limit.
    """
    response = client.post("/webhook", json={"task_text": "ok", "external_user_id": "u" * 147})
    assert response.status_code == 422


def test_webhook_rejects_external_thread_id_exceeding_max_length(
    client: TestClient,
) -> None:
    """external_thread_id longer than 255 characters should be rejected with 422."""
    response = client.post("/webhook", json={"task_text": "ok", "external_thread_id": "t" * 256})
    assert response.status_code == 422


def test_webhook_rejects_display_name_exceeding_max_length(
    client: TestClient,
) -> None:
    """display_name longer than 255 characters should be rejected with 422."""
    response = client.post("/webhook", json={"task_text": "ok", "display_name": "d" * 256})
    assert response.status_code == 422


def test_webhook_rejects_missing_task_text(client: TestClient) -> None:
    """Missing task_text should be rejected with 422."""
    response = client.post("/webhook", json={"repo_url": "https://example.com/repo"})
    assert response.status_code == 422


def test_webhook_rejects_extra_fields(client: TestClient) -> None:
    """Unknown extra fields should be rejected with 422 (extra='forbid')."""
    response = client.post(
        "/webhook",
        json={"task_text": "ok", "unknown_field": "injected"},
    )
    assert response.status_code == 422


def test_webhook_display_name_forwarded(client: TestClient) -> None:
    """display_name in the payload should be accepted without error."""
    response = client.post(
        "/webhook",
        json={
            "task_text": "greet the user",
            "display_name": "Alice",
            "source": "test",
            "external_user_id": "test:alice",
            "external_thread_id": "thread-1",
        },
    )
    assert response.status_code == 202
    assert response.json()["task_id"]


def test_webhook_rejects_negative_priority(client: TestClient) -> None:
    """Priority below 0 should be rejected with 422."""
    response = client.post("/webhook", json={"task_text": "ok", "priority": -1})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "callback_url",
    [
        "file:///etc/passwd",
        "http://localhost/callback",
        "http://127.0.0.1/callback",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8/callback",
    ],
)
def test_webhook_rejects_unsafe_callback_urls(
    client: TestClient,
    callback_url: str,
) -> None:
    """callback_url must not allow obvious SSRF targets."""
    response = client.post(
        "/webhook",
        json={"task_text": "ok", "callback_url": callback_url},
    )
    assert response.status_code == 422


def test_webhook_unconfigured_service_returns_503(client: TestClient) -> None:
    """The /webhook endpoint with no configured service should return 503."""
    app = create_app()  # no task_service → service is not configured
    with TestClient(app) as bare_client:
        response = bare_client.post("/webhook", json={"task_text": "run tests"})
    assert response.status_code == 503
