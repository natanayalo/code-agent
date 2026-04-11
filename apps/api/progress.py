"""Progress delivery adapters for task lifecycle updates."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from orchestrator.execution import (
    ProgressEvent,
    ProgressNotifier,
    TaskSubmission,
    _validate_callback_url,
)

logger = logging.getLogger(__name__)

_TELEGRAM_CHAT_ID_PATTERN = re.compile(r"^telegram:chat:(-?\d+)$")
_TELEGRAM_MESSAGE_LIMIT = 4096
_TELEGRAM_STARTED_PREFIX = "Task {task_id} started.\n\n"
_TELEGRAM_ELLIPSIS = "..."
_DEFAULT_OUTBOUND_TIMEOUT_SECONDS = 10.0


class HttpPostClient(Protocol):
    """Narrow async HTTP client surface required by the notifier adapters."""

    async def post(self, url: str, *, json: Any) -> httpx.Response:
        """Send a JSON POST request."""


@dataclass(frozen=True)
class OutboundHttpClients:
    """Shared async HTTP clients owned by the app lifespan."""

    telegram: httpx.AsyncClient
    webhook: httpx.AsyncClient


def create_outbound_http_clients(
    *,
    timeout_seconds: float = _DEFAULT_OUTBOUND_TIMEOUT_SECONDS,
) -> OutboundHttpClients:
    """Create shared async clients for outbound notifier delivery."""
    timeout = httpx.Timeout(timeout_seconds)
    return OutboundHttpClients(
        telegram=httpx.AsyncClient(timeout=timeout),
        webhook=httpx.AsyncClient(timeout=timeout),
    )


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

    def __init__(
        self,
        notifiers: Sequence[ProgressNotifier],
        *,
        timeout_seconds: float = _DEFAULT_OUTBOUND_TIMEOUT_SECONDS,
    ) -> None:
        self.notifiers = list(notifiers)
        self.timeout_seconds = timeout_seconds

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        await asyncio.gather(
            *(
                self._notify_one(notifier=notifier, submission=submission, event=event)
                for notifier in self.notifiers
            )
        )

    async def _notify_one(
        self,
        *,
        notifier: ProgressNotifier,
        submission: TaskSubmission,
        event: ProgressEvent,
    ) -> None:
        notifier_type = type(notifier).__name__
        base_log_extra = {
            "notifier_type": notifier_type,
            "task_id": event.task_id,
            "phase": event.phase,
        }
        try:
            await asyncio.wait_for(
                notifier.notify(submission=submission, event=event),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "Progress notification timed out for notifier",
                extra={**base_log_extra, "timeout_seconds": self.timeout_seconds},
            )
        except Exception:
            logger.warning(
                "Progress notification failed for notifier",
                extra=base_log_extra,
                exc_info=True,
            )


class TelegramProgressNotifier:
    """Send task lifecycle updates to Telegram chats."""

    def __init__(
        self,
        *,
        bot_token: str,
        client: HttpPostClient,
        api_base_url: str = "https://api.telegram.org",
    ) -> None:
        self.bot_token = bot_token
        self.client = client
        self.api_base_url = api_base_url.rstrip("/")

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        if event.channel != "telegram":
            return

        match = _TELEGRAM_CHAT_ID_PATTERN.match(event.external_thread_id)
        if match is None:
            raise ValueError(
                "Telegram progress delivery requires external_thread_id in telegram:chat:<id> form."
            )

        chat_id = int(match.group(1))
        response = await self.client.post(
            f"{self.api_base_url}/bot{self.bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": _format_telegram_message(event)},
        )
        response.raise_for_status()


class WebhookCallbackProgressNotifier:
    """POST task lifecycle updates to a caller-supplied callback URL."""

    def __init__(self, *, client: HttpPostClient) -> None:
        self.client = client

    async def notify(self, *, submission: TaskSubmission, event: ProgressEvent) -> None:
        if submission.callback_url is None:
            return

        # Re-validate just before each outbound delivery so DNS rebinding or
        # network topology changes after ingress validation fail closed.
        callback_url = _validate_callback_url(submission.callback_url)
        if callback_url is None:
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
        response = await self.client.post(callback_url, json=payload)
        response.raise_for_status()
