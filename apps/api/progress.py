"""Progress delivery adapters for task lifecycle updates."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

import httpx

from orchestrator.execution import ProgressEvent, ProgressNotifier, TaskSubmission

logger = logging.getLogger(__name__)

_TELEGRAM_CHAT_ID_PATTERN = re.compile(r"^telegram:chat:(-?\d+)$")
_TELEGRAM_MESSAGE_LIMIT = 4096
_TELEGRAM_STARTED_PREFIX = "Task {task_id} started.\n\n"
_TELEGRAM_ELLIPSIS = "..."


def _truncate_telegram_text(text: str, max_len: int) -> str:
    """Trim dynamic Telegram content so the final message fits the platform limit."""
    if len(text) <= max_len:
        return text
    truncated_len = max(max_len - len(_TELEGRAM_ELLIPSIS), 0)
    return text[:truncated_len] + _TELEGRAM_ELLIPSIS


def _format_telegram_message(event: ProgressEvent) -> str:
    """Render a compact Telegram message for one lifecycle event."""
    if event.phase == "started":
        prefix = _TELEGRAM_STARTED_PREFIX.format(task_id=event.task_id)
        detail = _truncate_telegram_text(
            event.task_text,
            _TELEGRAM_MESSAGE_LIMIT - len(prefix),
        )
        return f"{prefix}{detail}"
    if event.phase == "running":
        return f"Task {event.task_id} is running."
    phase_label = "completed" if event.phase == "completed" else "failed"
    prefix = f"Task {event.task_id} {phase_label}.\n\n"
    detail = event.summary or f"Task {phase_label}."
    truncated_detail = _truncate_telegram_text(
        detail,
        _TELEGRAM_MESSAGE_LIMIT - len(prefix),
    )
    return f"{prefix}{truncated_detail}"


class CompositeProgressNotifier:
    """Dispatch a progress event to multiple notifier backends."""

    def __init__(self, notifiers: Sequence[ProgressNotifier]) -> None:
        self.notifiers = list(notifiers)

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        for notifier in self.notifiers:
            try:
                await notifier.notify(submission=submission, event=event)
            except Exception:
                logger.warning(
                    "Progress notification failed for notifier",
                    extra={
                        "notifier_type": type(notifier).__name__,
                        "task_id": event.task_id,
                        "phase": event.phase,
                    },
                    exc_info=True,
                )


class TelegramProgressNotifier:
    """Send task lifecycle updates to Telegram chats."""

    def __init__(
        self,
        *,
        bot_token: str,
        api_base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.bot_token = bot_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        if event.channel != "telegram":
            return

        match = _TELEGRAM_CHAT_ID_PATTERN.match(event.external_thread_id)
        if match is None:
            raise ValueError(
                "Telegram progress delivery requires external_thread_id in telegram:chat:<id> form."
            )

        chat_id = int(match.group(1))
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.api_base_url}/bot{self.bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": _format_telegram_message(event)},
            )
            response.raise_for_status()


class WebhookCallbackProgressNotifier:
    """POST task lifecycle updates to a caller-supplied callback URL."""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        if submission.callback_url is None:
            return

        payload = {
            "task_id": event.task_id,
            "session_id": event.session_id,
            "phase": event.phase,
            "task_text": event.task_text,
            "summary": event.summary,
            "channel": event.channel,
            "external_thread_id": event.external_thread_id,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(submission.callback_url, json=payload)
            response.raise_for_status()
