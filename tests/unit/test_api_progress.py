"""Unit tests for outbound progress notifier adapters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api.progress import TelegramProgressNotifier, WebhookCallbackProgressNotifier
from orchestrator.execution import ProgressEvent, SubmissionSession, TaskSubmission


class _FakeAsyncClient:
    """Minimal async httpx client double that records POST requests."""

    def __init__(self, recorder: list[tuple[str, dict]]) -> None:
        self.recorder = recorder

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, json: dict) -> SimpleNamespace:
        self.recorder.append((url, json))
        return SimpleNamespace(raise_for_status=lambda: None)


@pytest.mark.anyio
async def test_telegram_progress_notifier_posts_send_message(monkeypatch) -> None:
    """Telegram progress updates should call the Telegram sendMessage API."""
    requests: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "apps.api.progress.httpx.AsyncClient",
        lambda timeout: _FakeAsyncClient(requests),
    )

    notifier = TelegramProgressNotifier(
        bot_token="token-123", api_base_url="https://tg.example.com"
    )
    submission = TaskSubmission(
        task_text="Run tests",
        session=SubmissionSession(
            channel="telegram",
            external_user_id="telegram:user:1",
            external_thread_id="telegram:chat:99",
        ),
    )
    event = ProgressEvent(
        phase="completed",
        task_id="task-1",
        session_id="session-1",
        channel="telegram",
        external_thread_id="telegram:chat:99",
        task_text="Run tests",
        summary="Done",
    )

    await notifier.notify(submission=submission, event=event)

    assert requests == [
        (
            "https://tg.example.com/bottoken-123/sendMessage",
            {"chat_id": 99, "text": "Task task-1 completed.\n\nDone"},
        )
    ]


@pytest.mark.anyio
async def test_webhook_callback_progress_notifier_posts_event_payload(monkeypatch) -> None:
    """Webhook progress updates should POST the task lifecycle payload to the callback URL."""
    requests: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "apps.api.progress.httpx.AsyncClient",
        lambda timeout: _FakeAsyncClient(requests),
    )

    notifier = WebhookCallbackProgressNotifier()
    submission = TaskSubmission(
        task_text="Run tests",
        callback_url="https://callbacks.example.com/status",
        session=SubmissionSession(
            channel="webhook:ci",
            external_user_id="webhook:ci:1",
            external_thread_id="thread-1",
        ),
    )
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="webhook:ci",
        external_thread_id="thread-1",
        task_text="Run tests",
    )

    await notifier.notify(submission=submission, event=event)

    assert requests == [
        (
            "https://callbacks.example.com/status",
            {
                "task_id": "task-1",
                "session_id": "session-1",
                "phase": "running",
                "task_text": "Run tests",
                "summary": None,
                "channel": "webhook:ci",
                "external_thread_id": "thread-1",
            },
        )
    ]
