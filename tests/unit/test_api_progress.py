"""Unit tests for outbound progress notifier adapters."""

import asyncio

import httpx
import pytest

from apps.api.progress import (
    CompositeProgressNotifier,
    TelegramProgressNotifier,
    WebhookCallbackProgressNotifier,
    _format_telegram_message,
)
from orchestrator.execution import ProgressEvent, SubmissionSession, TaskSubmission


class _FakeAsyncClient:
    """Minimal async httpx client double that records POST requests."""

    def __init__(self, recorder: list[tuple[str, dict]]) -> None:
        self.recorder = recorder

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        self.recorder.append((url, json))
        request = httpx.Request("POST", url, json=json)
        return httpx.Response(200, request=request)


class _FailingNotifier:
    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        raise RuntimeError("boom")


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls = 0
        self.notified = asyncio.Event()

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        self.calls += 1
        self.notified.set()


class _BlockingNotifier:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        self.started.set()
        await self.release.wait()


@pytest.mark.anyio
async def test_telegram_progress_notifier_posts_send_message() -> None:
    """Telegram progress updates should call the Telegram sendMessage API."""
    requests: list[tuple[str, dict]] = []

    notifier = TelegramProgressNotifier(
        bot_token="token-123",
        client=_FakeAsyncClient(requests),
        api_base_url="https://tg.example.com",
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
async def test_webhook_callback_progress_notifier_posts_event_payload() -> None:
    """Webhook progress updates should POST the task lifecycle payload to the callback URL."""
    requests: list[tuple[str, dict]] = []

    notifier = WebhookCallbackProgressNotifier(client=_FakeAsyncClient(requests))
    submission = TaskSubmission(
        task_text="Run tests",
        callback_url="https://93.184.216.34/status",
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
            "https://93.184.216.34/status",
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


@pytest.mark.anyio
async def test_webhook_callback_progress_notifier_revalidates_callback_target(monkeypatch) -> None:
    """Delivery should re-check callback safety before posting outbound events."""
    requests: list[tuple[str, dict]] = []
    validated_urls: list[str] = []

    def fake_validate_callback_url(value: str | None) -> str | None:
        assert value is not None
        validated_urls.append(value)
        return value

    monkeypatch.setattr(
        "apps.api.progress.validate_callback_url",
        fake_validate_callback_url,
    )

    notifier = WebhookCallbackProgressNotifier(client=_FakeAsyncClient(requests))
    submission = TaskSubmission(
        task_text="Run tests",
        callback_url="https://93.184.216.34/status",
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

    assert validated_urls == ["https://93.184.216.34/status"]
    assert len(requests) == 1


@pytest.mark.anyio
async def test_webhook_callback_progress_notifier_fails_closed_on_revalidation_error(
    monkeypatch,
) -> None:
    """Notifier should fail closed when delivery-time callback validation rejects the target."""
    requests: list[tuple[str, dict]] = []

    def fake_validate_callback_url(value: str | None) -> str | None:
        raise ValueError("callback_url must not target a private or local address.")

    monkeypatch.setattr(
        "apps.api.progress.validate_callback_url",
        fake_validate_callback_url,
    )

    notifier = WebhookCallbackProgressNotifier(client=_FakeAsyncClient(requests))
    submission = TaskSubmission(
        task_text="Run tests",
        callback_url="https://93.184.216.34/status",
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

    with pytest.raises(ValueError, match="private or local address"):
        await notifier.notify(submission=submission, event=event)

    assert requests == []


def test_format_telegram_message_truncates_started_text_to_platform_limit() -> None:
    """Started messages should be truncated to stay within Telegram's 4096-char limit."""
    event = ProgressEvent(
        phase="started",
        task_id="task-1",
        session_id="session-1",
        channel="telegram",
        external_thread_id="telegram:chat:99",
        task_text="x" * 10_000,
    )

    message = _format_telegram_message(event)

    assert len(message) == 4096
    assert message.endswith("...")


def test_format_telegram_message_truncates_completed_summary_to_platform_limit() -> None:
    """Completed messages should also be truncated to stay within Telegram's limit."""
    event = ProgressEvent(
        phase="completed",
        task_id="task-1",
        session_id="session-1",
        channel="telegram",
        external_thread_id="telegram:chat:99",
        task_text="Run tests",
        summary="y" * 10_000,
    )

    message = _format_telegram_message(event)

    assert len(message) == 4096
    assert message.startswith("Task task-1 completed.\n\n")
    assert message.endswith("...")


def test_format_telegram_message_for_running_phase() -> None:
    """Running phase should render the concise in-progress message."""
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="telegram",
        external_thread_id="telegram:chat:99",
        task_text="Run tests",
    )

    assert _format_telegram_message(event) == "Task task-1 is running."


def test_format_telegram_message_for_awaiting_approval_phase() -> None:
    """Approval-paused phase should avoid a failure label in Telegram output."""
    event = ProgressEvent(
        phase="awaiting_approval",
        task_id="task-1",
        session_id="session-1",
        channel="telegram",
        external_thread_id="telegram:chat:99",
        task_text="Run tests",
        summary="Run paused pending permission escalation approval.",
    )

    assert _format_telegram_message(event) == (
        "Task task-1 is awaiting approval.\n\n" "Run paused pending permission escalation approval."
    )


@pytest.mark.anyio
async def test_telegram_progress_notifier_ignores_non_telegram_channels() -> None:
    """Telegram notifier should no-op for non-telegram events."""
    requests: list[tuple[str, dict]] = []
    notifier = TelegramProgressNotifier(
        bot_token="token-123",
        client=_FakeAsyncClient(requests),
    )
    submission = TaskSubmission(task_text="Run tests")
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="webhook:ci",
        external_thread_id="thread-1",
        task_text="Run tests",
    )

    await notifier.notify(submission=submission, event=event)

    assert requests == []


@pytest.mark.anyio
async def test_telegram_progress_notifier_requires_chat_id_format() -> None:
    """Telegram notifier should fail clearly when thread id is not telegram:chat:<id>."""
    notifier = TelegramProgressNotifier(
        bot_token="token-123",
        client=_FakeAsyncClient([]),
    )
    submission = TaskSubmission(task_text="Run tests")
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="telegram",
        external_thread_id="thread-1",
        task_text="Run tests",
    )

    with pytest.raises(ValueError, match="telegram:chat:<id>"):
        await notifier.notify(submission=submission, event=event)


@pytest.mark.anyio
async def test_webhook_callback_progress_notifier_skips_missing_callback() -> None:
    """Webhook notifier should no-op when callback_url is absent."""
    requests: list[tuple[str, dict]] = []
    notifier = WebhookCallbackProgressNotifier(client=_FakeAsyncClient(requests))
    submission = TaskSubmission(task_text="Run tests", callback_url=None)
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="webhook:ci",
        external_thread_id="thread-1",
        task_text="Run tests",
    )

    await notifier.notify(submission=submission, event=event)

    assert requests == []


@pytest.mark.anyio
async def test_webhook_callback_progress_notifier_skips_when_revalidation_returns_none(
    monkeypatch,
) -> None:
    """Webhook notifier should no-op when delivery-time validation returns None."""
    requests: list[tuple[str, dict]] = []
    notifier = WebhookCallbackProgressNotifier(client=_FakeAsyncClient(requests))
    submission = TaskSubmission(
        task_text="Run tests",
        callback_url="https://93.184.216.34/status",
    )
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="webhook:ci",
        external_thread_id="thread-1",
        task_text="Run tests",
    )

    monkeypatch.setattr("apps.api.progress.validate_callback_url", lambda _value: None)

    await notifier.notify(submission=submission, event=event)

    assert requests == []


@pytest.mark.anyio
async def test_composite_progress_notifier_logs_and_continues_after_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One notifier failure should not stop later progress notifiers from running."""
    notifier = _RecordingNotifier()
    composite = CompositeProgressNotifier([_FailingNotifier(), notifier])
    submission = TaskSubmission(task_text="Run tests")
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="webhook:ci",
        external_thread_id="thread-1",
        task_text="Run tests",
    )

    with caplog.at_level("WARNING"):
        await composite.notify(submission=submission, event=event)

    assert notifier.calls == 1
    assert "Progress notification failed for notifier" in caplog.text
    assert caplog.records[0].notifier_type == "_FailingNotifier"


@pytest.mark.anyio
async def test_composite_progress_notifier_runs_backends_in_parallel() -> None:
    """A slow notifier should not block sibling deliveries from starting."""
    blocking_notifier = _BlockingNotifier()
    notifier = _RecordingNotifier()
    composite = CompositeProgressNotifier(
        [blocking_notifier, notifier],
        timeout_seconds=1.0,
    )
    submission = TaskSubmission(task_text="Run tests")
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="webhook:ci",
        external_thread_id="thread-1",
        task_text="Run tests",
    )

    notify_task = asyncio.create_task(composite.notify(submission=submission, event=event))
    await blocking_notifier.started.wait()
    await notifier.notified.wait()

    assert notifier.calls == 1

    blocking_notifier.release.set()
    await notify_task


@pytest.mark.anyio
async def test_composite_progress_notifier_times_out_one_backend_without_blocking_siblings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One stuck backend should time out without suppressing sibling deliveries."""
    blocking_notifier = _BlockingNotifier()
    notifier = _RecordingNotifier()
    composite = CompositeProgressNotifier(
        [blocking_notifier, notifier],
        timeout_seconds=0.01,
    )
    submission = TaskSubmission(task_text="Run tests")
    event = ProgressEvent(
        phase="running",
        task_id="task-1",
        session_id="session-1",
        channel="webhook:ci",
        external_thread_id="thread-1",
        task_text="Run tests",
    )

    with caplog.at_level("WARNING"):
        await composite.notify(submission=submission, event=event)

    assert notifier.calls == 1
    assert "Progress notification timed out for notifier" in caplog.text
    assert caplog.records[0].notifier_type == "_BlockingNotifier"
