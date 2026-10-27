"""Bounded API-process monitor for readiness transition Telegram alerts."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import sessionmaker

from repositories import ReadinessAlertRepository, session_scope

logger = logging.getLogger(__name__)

ALERT_CHAT_ID_ENV_VAR = "CODE_AGENT_READINESS_ALERT_TELEGRAM_CHAT_ID"
ALERT_INTERVAL_SECONDS = 30
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 5
MAX_UNPERSISTED_SNAPSHOTS = 100
_CHAT_ID_PATTERN = re.compile(r"^-?\d+$")
_LOCAL_RECORD_LOCK = threading.Lock()
_SAFE_ACTIONS = {
    "postgres_unavailable": "Check Postgres connectivity, then confirm /ready recovers.",
    "temporal_unavailable": "Check Temporal health and confirm /ready recovers.",
    "worker_unavailable": "Check the Temporal worker and its heartbeat.",
    "dispatcher_unavailable": "Check the worker command dispatcher heartbeat.",
    "dispatcher_backlog_stale": "Inspect the Temporal command outbox and worker dispatcher.",
}


def readiness_alert_configuration(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """Return destination and bot token only when both safe settings are valid."""
    source = environ if environ is not None else os.environ
    chat_id = str(source.get(ALERT_CHAT_ID_ENV_VAR, "")).strip()
    bot_token = str(source.get("CODE_AGENT_TELEGRAM_BOT_TOKEN", "")).strip()
    if not chat_id or not bot_token:
        return None
    if not _CHAT_ID_PATTERN.fullmatch(chat_id):
        logger.warning("Readiness alerts disabled: Telegram chat ID is invalid")
        return None
    return chat_id, bot_token


def format_readiness_alert(reason: str, transition: str) -> str:
    """Format a bounded message from stable codes only; never include probe details."""
    action = _SAFE_ACTIONS.get(reason, "Inspect authenticated readiness status and /ready.")
    state = "degraded" if transition == "degraded" else "recovered"
    return f"Execution readiness {state}: {reason}\nNext action: {action}"


class ReadinessAlertMonitor:
    """Probe readiness independently and deliver persisted transitions with retries."""

    def __init__(
        self,
        *,
        task_service: Any,
        session_factory: sessionmaker,
        telegram_client: Any,
        bot_token: str,
        chat_id: str,
        api_base_url: str = "https://api.telegram.org",
        interval_seconds: int = ALERT_INTERVAL_SECONDS,
        readiness_probe: Callable[[Any], Any] | None = None,
    ) -> None:
        self.task_service = task_service
        self.session_factory = session_factory
        self.telegram_client = telegram_client
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base_url = api_base_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.readiness_probe = readiness_probe or (lambda service: service.get_readiness())
        self._task: asyncio.Task[None] | None = None
        self._unpersisted_snapshots: list[list[str]] = []

    def start(self) -> None:
        """Start the monitor once; it runs only in the API process."""
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="readiness-alert-monitor")

    async def stop(self) -> None:
        """Stop promptly during API shutdown."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run(self) -> None:
        """Poll readiness at a fixed bounded interval until cancelled."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Readiness alert iteration failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.interval_seconds)

    async def run_once(self) -> None:
        """Persist one readiness sample, then attempt a bounded due batch."""
        snapshot = await asyncio.to_thread(self.readiness_probe, self.task_service)
        reasons = sorted(set(snapshot.degraded_reasons))
        try:
            for pending in self._unpersisted_snapshots:
                await asyncio.to_thread(self._record_snapshot, pending)
            await asyncio.to_thread(self._record_snapshot, reasons)
        except Exception as exc:
            if not self._unpersisted_snapshots or self._unpersisted_snapshots[-1] != reasons:
                self._unpersisted_snapshots.append(reasons)
                if len(self._unpersisted_snapshots) > MAX_UNPERSISTED_SNAPSHOTS:
                    self._unpersisted_snapshots.pop(1)
            logger.warning("Readiness alert state persistence failed (%s)", type(exc).__name__)
            return
        self._unpersisted_snapshots.clear()
        batch = await asyncio.to_thread(self._claim_batch)
        for delivery in batch:
            await self._deliver(delivery)

    def _record_snapshot(self, reasons: list[str]) -> None:
        with _LOCAL_RECORD_LOCK, session_scope(self.session_factory) as session:
            ReadinessAlertRepository(session).record_snapshot(reasons)

    def _claim_batch(self) -> list[dict[str, Any]]:
        with session_scope(self.session_factory) as session:
            claimed = ReadinessAlertRepository(session).claim_due()
            return [
                {
                    "id": item.id,
                    "reason": item.reason,
                    "transition": item.transition,
                    "attempts": item.attempts,
                }
                for item in claimed
            ]

    async def _deliver(self, item: dict[str, Any]) -> None:
        url = f"{self.api_base_url}/bot{self.bot_token}/sendMessage"
        http_status: int | None = None
        try:
            response = await asyncio.wait_for(
                self.telegram_client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": format_readiness_alert(item["reason"], item["transition"]),
                    },
                    timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
                ),
                timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )
            http_status = response.status_code
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("ok") is False:
                raise ValueError("Telegram rejected the message")
        except Exception as exc:
            await asyncio.to_thread(self._mark_failed, item["id"], http_status)
            logger.warning(
                "Readiness Telegram delivery failed reason=%s transition=%s status=%s error=%s",
                item["reason"],
                item["transition"],
                http_status,
                type(exc).__name__,
            )
            return
        await asyncio.to_thread(self._mark_delivered, item["id"], http_status or 200)

    def _mark_failed(self, delivery_id: str, http_status: int | None) -> None:
        with session_scope(self.session_factory) as session:
            ReadinessAlertRepository(session).mark_failed(delivery_id, http_status=http_status)

    def _mark_delivered(self, delivery_id: str, http_status: int) -> None:
        with session_scope(self.session_factory) as session:
            ReadinessAlertRepository(session).mark_delivered(delivery_id, http_status=http_status)
