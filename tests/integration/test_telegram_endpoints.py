"""Integration tests for the Telegram webhook adapter (T-051)."""

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
    """Worker double that returns a canned result and records calls."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


@pytest.fixture
def session_factory():
    """SQLite-backed session factory for Telegram endpoint tests."""
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
    worker = StaticWorker(
        WorkerResult(
            status="success",
            summary="Telegram task completed.",
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
        )
    )
    app.state.test_worker = worker
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Minimal Telegram Update fixtures
# ---------------------------------------------------------------------------


def _text_update(text: str, update_id: int = 1, user_id: int = 42, chat_id: int = 100) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id},
            "from": {"id": user_id, "first_name": "Alice"},
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_telegram_text_message_creates_task(client: TestClient) -> None:
    """A Telegram Update with a text message should create a task and return ok=True."""
    response = client.post("/telegram/webhook", json=_text_update("Run the linter"))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["task_id"] is not None


def test_telegram_task_text_propagated_to_worker(client: TestClient) -> None:
    """The message text should arrive at the worker unchanged."""
    client.post("/telegram/webhook", json=_text_update("Create a README"))

    worker = client.app.state.test_worker
    assert len(worker.requests) == 1
    assert worker.requests[0].task_text == "Create a README"


def test_telegram_same_chat_reuses_session(client: TestClient) -> None:
    """Two messages from the same chat should share the same session_id."""
    r1 = client.post("/telegram/webhook", json=_text_update("task one", update_id=1))
    r2 = client.post("/telegram/webhook", json=_text_update("task two", update_id=2))

    assert r1.json()["ok"] is True
    assert r2.json()["ok"] is True
    assert r1.json()["session_id"] == r2.json()["session_id"]


def test_telegram_different_chats_get_different_sessions(client: TestClient) -> None:
    """Messages from different chats must produce different session IDs."""
    r1 = client.post("/telegram/webhook", json=_text_update("task", update_id=1, chat_id=100))
    r2 = client.post("/telegram/webhook", json=_text_update("task", update_id=2, chat_id=200))

    assert r1.json()["session_id"] != r2.json()["session_id"]


def test_telegram_task_persisted(client: TestClient, session_factory) -> None:
    """A submitted Telegram task should be persisted with COMPLETED status."""
    response = client.post("/telegram/webhook", json=_text_update("fix the tests"))
    task_id = response.json()["task_id"]

    get_resp = client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "completed"

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.status is TaskStatus.COMPLETED


def test_telegram_display_name_from_first_last(client: TestClient) -> None:
    """first_name + last_name should produce a combined display_name."""
    update = {
        "update_id": 10,
        "message": {
            "message_id": 10,
            "chat": {"id": 1},
            "from": {"id": 5, "first_name": "John", "last_name": "Doe"},
            "text": "hello",
        },
    }
    response = client.post("/telegram/webhook", json=update)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_telegram_update_with_username_only(client: TestClient) -> None:
    """A user with only a username (no first/last name) should be accepted."""
    update = {
        "update_id": 11,
        "message": {
            "message_id": 11,
            "chat": {"id": 2},
            "from": {"id": 6, "username": "bot_user"},
            "text": "deploy",
        },
    }
    response = client.post("/telegram/webhook", json=update)
    assert response.status_code == 200
    assert response.json()["task_id"] is not None


def test_telegram_channel_post_no_sender(client: TestClient) -> None:
    """Channel posts (no 'from' field) should still create a task."""
    update = {
        "update_id": 12,
        "channel_post": {
            "message_id": 12,
            "chat": {"id": 3},
            "text": "channel broadcast task",
        },
    }
    response = client.post("/telegram/webhook", json=update)
    assert response.status_code == 200
    assert response.json()["task_id"] is not None


# ---------------------------------------------------------------------------
# Silent-ignore tests (no task created, 200 returned)
# ---------------------------------------------------------------------------


def test_telegram_update_without_message_returns_ok(client: TestClient) -> None:
    """Non-message updates (e.g. poll, callback) should return ok=True with no task."""
    response = client.post("/telegram/webhook", json={"update_id": 99})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["task_id"] is None
    assert response.json()["detail"] == "no_message"
    # No task should have been created.
    worker = client.app.state.test_worker
    assert len(worker.requests) == 0


def test_telegram_message_without_text_returns_ok(client: TestClient) -> None:
    """Photo/sticker messages with no text field should return ok=True with no task."""
    update = {
        "update_id": 50,
        "message": {
            "message_id": 50,
            "chat": {"id": 1},
            "from": {"id": 1},
        },
    }
    response = client.post("/telegram/webhook", json=update)

    assert response.status_code == 200
    assert response.json()["detail"] == "no_text"
    assert response.json()["task_id"] is None
    worker = client.app.state.test_worker
    assert len(worker.requests) == 0


def test_telegram_message_with_whitespace_only_text_returns_ok(client: TestClient) -> None:
    """A message whose text is only whitespace should be ignored."""
    response = client.post("/telegram/webhook", json=_text_update("   "))

    assert response.status_code == 200
    assert response.json()["detail"] == "no_text"
    worker = client.app.state.test_worker
    assert len(worker.requests) == 0


def test_telegram_message_exceeding_max_length_returns_ok(client: TestClient) -> None:
    """A message longer than 10 000 chars should be silently ignored (not a 4xx)."""
    response = client.post("/telegram/webhook", json=_text_update("x" * 10_001))

    assert response.status_code == 200
    assert response.json()["detail"] == "text_too_long"
    worker = client.app.state.test_worker
    assert len(worker.requests) == 0


# ---------------------------------------------------------------------------
# Service availability
# ---------------------------------------------------------------------------


def test_telegram_unconfigured_service_returns_503(client: TestClient) -> None:
    """Telegram endpoint with no configured service should return 503."""
    app = create_app()
    with TestClient(app) as bare:
        response = bare.post("/telegram/webhook", json=_text_update("hello"))
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Unknown fields are tolerated (Telegram adds fields without notice)
# ---------------------------------------------------------------------------


def test_telegram_update_with_unknown_fields_accepted(client: TestClient) -> None:
    """Extra fields in the Update object must not cause a 422."""
    update = _text_update("ok")
    update["unknown_future_field"] = {"data": 123}
    update["message"]["unknown_message_field"] = True

    response = client.post("/telegram/webhook", json=update)
    assert response.status_code == 200
    assert response.json()["task_id"] is not None
