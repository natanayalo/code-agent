"""Bounded API-process monitor for readiness transition Telegram alerts."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from db.base import utc_now
from orchestrator.operational_health import (
    OUTBOX_STALE_SECONDS,
    TerminalReconciliationAlertSnapshot,
    get_stuck_interaction_task_ids,
    get_terminal_reconciliation_alert_snapshot,
)
from orchestrator.operational_health_types import READINESS_REASON_CODES
from orchestrator.provider_observations import ACTIONS, alert_identity, fresh_observations
from repositories import ReadinessAlertRepository, session_scope
from repositories.sqlalchemy_operational_health import OperationalHealthRepository
from repositories.sqlalchemy_readiness_alerts import (
    INTERACTION_REASON_PREFIX,
    OUTBOX_STALE_REASON_PREFIX,
    TERMINAL_RECONCILIATION_REASON_PREFIX,
)

logger = logging.getLogger(__name__)

ALERT_CHAT_ID_ENV_VAR = "CODE_AGENT_READINESS_ALERT_TELEGRAM_CHAT_ID"
ALERT_INTERVAL_SECONDS = 30
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 5
MAX_UNPERSISTED_SNAPSHOTS = 100
MAX_DELIVERIES_PER_CYCLE = 20
_CHAT_ID_PATTERN = re.compile(r"^-?\d+$")
_LOCAL_DB_LOCK = threading.Lock()
_UNKNOWN_COMPONENT_REASONS = {
    "postgres": ("postgres_unavailable",),
    "temporal": ("temporal_unavailable",),
    "worker": ("worker_unavailable",),
    "dispatcher": ("dispatcher_unavailable", "dispatcher_backlog_stale"),
}
_SAFE_ACTIONS = {
    "postgres_unavailable": "Check Postgres connectivity, then confirm /ready recovers.",
    "temporal_unavailable": "Check Temporal health and confirm /ready recovers.",
    "worker_unavailable": "Check the Temporal worker and its heartbeat.",
    "dispatcher_unavailable": "Check the worker command dispatcher heartbeat.",
    "dispatcher_backlog_stale": "Inspect the Temporal command outbox and worker dispatcher.",
    "outbox_command_stale": "Inspect the Temporal command outbox and worker dispatcher.",
    "terminal_reconciliation_divergent": "Inspect the task and its Temporal workflow.",
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
    identity = alert_identity(reason)
    if identity:
        worker_id, provider, capability = identity
        state = "degraded" if transition == "degraded" else "recovered"
        return (
            f"Provider capability {state}: {capability}\nWorker: {worker_id}"
            f"\nProvider: {provider}\nNext action: {ACTIONS[capability]}"
        )
    if reason.startswith(INTERACTION_REASON_PREFIX):
        task_id = reason.removeprefix(INTERACTION_REASON_PREFIX)
        state = "degraded" if transition == "degraded" else "recovered"
        return (
            f"Execution alert {state}: interaction_wait_stuck\nTask: {task_id}"
            "\nNext action: Answer, reject, or cancel the pending interaction "
            "through the dashboard or API."
        )
    if reason.startswith(OUTBOX_STALE_REASON_PREFIX):
        task_id = reason.removeprefix(OUTBOX_STALE_REASON_PREFIX)
        state = "degraded" if transition == "degraded" else "recovered"
        return (
            f"Execution alert {state}: outbox_command_stale\nTask: {task_id}"
            "\nNext action: Inspect the Temporal command outbox and worker dispatcher."
        )
    if reason.startswith(TERMINAL_RECONCILIATION_REASON_PREFIX):
        task_id = reason.removeprefix(TERMINAL_RECONCILIATION_REASON_PREFIX)
        state = "degraded" if transition == "degraded" else "recovered"
        return (
            f"Execution alert {state}: terminal_reconciliation_divergent\nTask: {task_id}"
            f"\nNext action: {_SAFE_ACTIONS['terminal_reconciliation_divergent']}"
        )
    action = _SAFE_ACTIONS.get(reason, "Inspect authenticated readiness status and /ready.")
    state = "degraded" if transition == "degraded" else "recovered"
    return f"Execution readiness {state}: {reason}\nNext action: {action}"


def _unknown_reason_codes(snapshot: Any) -> list[str]:
    """Return reason codes whose source components were not observable this sample."""
    components = getattr(snapshot, "components", {})
    unknown: set[str] = set()
    for component_name, reason_codes in _UNKNOWN_COMPONENT_REASONS.items():
        component = components.get(component_name)
        if getattr(component, "status", None) == "unknown":
            unknown.update(reason_codes)
    return sorted(unknown)


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
        interaction_probe: Callable[[Any, datetime], list[str]] | None = None,
        outbox_probe: Callable[[Any, datetime], list[str]] | None = None,
        reconciliation_probe: (
            Callable[[Any, datetime], list[str] | TerminalReconciliationAlertSnapshot] | None
        ) = None,
    ) -> None:
        self.task_service = task_service
        self.session_factory = session_factory
        self.telegram_client = telegram_client
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base_url = api_base_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.readiness_probe = readiness_probe or (lambda service: service.get_readiness())
        self.interaction_probe = interaction_probe or get_stuck_interaction_task_ids
        self.outbox_probe = outbox_probe or self._probe_stale_outbox_task_ids
        self.reconciliation_probe = (
            reconciliation_probe or get_terminal_reconciliation_alert_snapshot
        )
        self._task: asyncio.Task[None] | None = None
        self._unpersisted_snapshots: list[tuple[list[str], list[str], datetime]] = []

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
        unknown_reasons = _unknown_reason_codes(snapshot)
        observed_at = getattr(snapshot, "checked_at", None) or utc_now()
        current_sample = (reasons, unknown_reasons, observed_at)
        self._buffer_snapshot(current_sample)
        try:
            pending_samples = list(self._unpersisted_snapshots)
            await asyncio.to_thread(self._record_snapshots, pending_samples)
        except Exception as exc:
            logger.warning("Readiness alert state persistence failed (%s)", type(exc).__name__)
            return
        self._unpersisted_snapshots.clear()
        try:
            interaction_observed_at = utc_now()
            task_ids = await asyncio.to_thread(
                self.interaction_probe, self.task_service, interaction_observed_at
            )
            await asyncio.to_thread(
                self._record_interaction_snapshot, task_ids, interaction_observed_at
            )
        except Exception as exc:
            logger.warning("Interaction alert observation failed (%s)", type(exc).__name__)
        try:
            outbox_observed_at = utc_now()
            task_ids = await asyncio.to_thread(
                self.outbox_probe, self.task_service, outbox_observed_at
            )
            await asyncio.to_thread(
                self._record_stale_outbox_snapshot, task_ids, outbox_observed_at
            )
        except Exception as exc:
            logger.warning("Stale outbox alert observation failed (%s)", type(exc).__name__)
        try:
            reconciliation_observed_at = utc_now()
            observation = await asyncio.to_thread(
                self.reconciliation_probe, self.task_service, reconciliation_observed_at
            )
            if isinstance(observation, TerminalReconciliationAlertSnapshot):
                task_ids = list(observation.divergent_task_ids)
                unconfirmed_task_ids = list(observation.unconfirmed_task_ids)
            else:
                task_ids = observation
                unconfirmed_task_ids = []
            await asyncio.to_thread(
                self._record_terminal_reconciliation_snapshot,
                task_ids,
                reconciliation_observed_at,
                unconfirmed_task_ids,
            )
        except Exception as exc:
            logger.warning(
                "Terminal reconciliation alert observation failed (%s)", type(exc).__name__
            )
        try:
            await asyncio.to_thread(self._record_provider_observations)
        except Exception as exc:
            logger.warning("Provider alert observation failed (%s)", type(exc).__name__)
        for _ in range(MAX_DELIVERIES_PER_CYCLE):
            delivery = await asyncio.to_thread(self._claim_one)
            if delivery is None:
                break
            await self._deliver(delivery)

    def _record_provider_observations(self) -> None:
        with _LOCAL_DB_LOCK, session_scope(self.session_factory) as session:
            ReadinessAlertRepository(session).record_provider_observations(
                fresh_observations(session, utc_now())
            )

    def _buffer_snapshot(self, sample: tuple[list[str], list[str], datetime]) -> None:
        reasons, unknown_reasons, _observed_at = sample
        if self._unpersisted_snapshots:
            last_reasons, last_unknown, _last_observed_at = self._unpersisted_snapshots[-1]
            if last_reasons == reasons and last_unknown == unknown_reasons:
                self._unpersisted_snapshots[-1] = sample
                return
        self._unpersisted_snapshots.append(sample)
        if len(self._unpersisted_snapshots) > MAX_UNPERSISTED_SNAPSHOTS:
            self._unpersisted_snapshots.pop(1)

    def _record_snapshots(self, samples: list[tuple[list[str], list[str], datetime]]) -> None:
        """Persist the buffered replay atomically to keep its transitions ordered."""
        with _LOCAL_DB_LOCK, session_scope(self.session_factory) as session:
            repository = ReadinessAlertRepository(session)
            latest_states = repository.capture_observed_states()
            repository.record_earlier_incidents(samples, captured=latest_states)
            latest_replayed_at: dict[str, datetime] = {}
            for reasons, unknown_reasons, observed_at in samples:
                repository.record_snapshot(
                    reasons, unknown_reasons=unknown_reasons, observed_at=observed_at
                )
                for reason in set(READINESS_REASON_CODES) - set(unknown_reasons):
                    latest_replayed_at[reason] = observed_at
            repository.restore_newer_observations(latest_states, latest_replayed_at)

    def _claim_one(self) -> dict[str, Any] | None:
        with _LOCAL_DB_LOCK, session_scope(self.session_factory) as session:
            claimed = ReadinessAlertRepository(session).claim_due(limit=1)
            if not claimed:
                return None
            item = claimed[0]
            return {
                "id": item.id,
                "reason": item.reason,
                "transition": item.transition,
                "attempts": item.attempts,
            }

    def _record_interaction_snapshot(self, task_ids: list[str], observed_at: datetime) -> None:
        with _LOCAL_DB_LOCK, session_scope(self.session_factory) as session:
            ReadinessAlertRepository(session).record_interaction_snapshot(
                task_ids, observed_at=observed_at
            )

    def _probe_stale_outbox_task_ids(self, task_service: Any, observed_at: datetime) -> list[str]:
        """Read the complete stale eligible task set directly from the outbox table."""
        with session_scope(task_service.session_factory) as session:
            return OperationalHealthRepository(session).stale_outbox_task_ids(
                now=observed_at, stale_seconds=OUTBOX_STALE_SECONDS
            )

    def _record_stale_outbox_snapshot(self, task_ids: list[str], observed_at: datetime) -> None:
        with _LOCAL_DB_LOCK, session_scope(self.session_factory) as session:
            ReadinessAlertRepository(session).record_stale_outbox_snapshot(
                task_ids, observed_at=observed_at
            )

    def _record_terminal_reconciliation_snapshot(
        self,
        task_ids: list[str],
        observed_at: datetime,
        unconfirmed_task_ids: list[str] | None = None,
    ) -> None:
        with _LOCAL_DB_LOCK, session_scope(self.session_factory) as session:
            ReadinessAlertRepository(session).record_terminal_reconciliation_snapshot(
                task_ids,
                observed_at=observed_at,
                unconfirmed_task_ids=unconfirmed_task_ids,
            )

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
