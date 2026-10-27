"""Integration coverage for readiness alert transitions and operator inspection."""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from apps.api.readiness_alerts import (
    ALERT_CHAT_ID_ENV_VAR,
    ReadinessAlertMonitor,
    format_readiness_alert,
    readiness_alert_configuration,
)
from db.base import utc_now
from db.readiness_alert_models import ReadinessAlertDelivery
from orchestrator.execution import TaskExecutionService
from repositories import ReadinessAlertRepository, session_scope
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET, _default_worker


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("HTTP failure")

    def json(self) -> dict[str, bool]:
        return {"ok": self.status_code < 400}


class FakeTelegram:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [200])
        self.messages: list[dict] = []

    async def post(self, url: str, *, json: dict, timeout: int) -> FakeResponse:
        self.messages.append({"url": url, **json})
        return FakeResponse(self.statuses.pop(0) if self.statuses else 200)


def _snapshot(*reasons: str):
    return SimpleNamespace(degraded_reasons=list(reasons))


def _delivery_rows(session_factory) -> list[ReadinessAlertDelivery]:
    with session_scope(session_factory) as session:
        return ReadinessAlertRepository(session).list_deliveries()


def test_readiness_alerts_deduplicate_and_emit_recovery(session_factory) -> None:
    reasons = ["temporal_unavailable"]
    telegram = FakeTelegram()
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token-secret",
        chat_id="-10012",
        readiness_probe=lambda _service: _snapshot(*reasons),
    )

    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())
    reasons.clear()
    asyncio.run(monitor.run_once())

    deliveries = _delivery_rows(session_factory)
    assert sorted((row.reason, row.transition, row.status) for row in deliveries) == [
        ("temporal_unavailable", "degraded", "delivered"),
        ("temporal_unavailable", "recovered", "delivered"),
    ]
    assert len(telegram.messages) == 2
    assert all("token-secret" not in message["text"] for message in telegram.messages)
    assert all("task" not in message["text"].lower() for message in telegram.messages)


def test_readiness_alert_retry_is_bounded_and_visible(session_factory) -> None:
    telegram = FakeTelegram([503])
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: _snapshot("worker_unavailable"),
    )
    asyncio.run(monitor.run_once())
    failed = _delivery_rows(session_factory)[0]
    assert failed.status == "pending"
    assert failed.attempts == 1
    assert failed.last_failure_code == "telegram_request_failed"
    assert failed.last_http_status == 503
    assert failed.next_attempt_at.replace(tzinfo=UTC) > utc_now()

    with session_scope(session_factory) as session:
        row = session.get(ReadinessAlertDelivery, failed.id)
        row.next_attempt_at = utc_now() - timedelta(seconds=1)
        row.attempts = 7
    telegram.statuses = [503]
    asyncio.run(monitor.run_once())
    exhausted = _delivery_rows(session_factory)[0]
    assert exhausted.status == "failed"
    assert exhausted.attempts == 8
    assert exhausted.last_failure_code == "telegram_request_failed"


def test_concurrent_monitors_create_one_transition_delivery(session_factory) -> None:
    telegram = FakeTelegram()
    monitors = [
        ReadinessAlertMonitor(
            task_service=object(),
            session_factory=session_factory,
            telegram_client=telegram,
            bot_token="token",
            chat_id="123",
            readiness_probe=lambda _service: _snapshot("dispatcher_unavailable"),
        )
        for _ in range(2)
    ]

    async def run_both() -> None:
        await asyncio.gather(*(monitor.run_once() for monitor in monitors))

    asyncio.run(run_both())
    deliveries = _delivery_rows(session_factory)
    assert len(deliveries) == 1
    assert len(telegram.messages) == 1


def test_postgres_outage_observed_while_unavailable_is_reported_after_recovery(
    session_factory,
) -> None:
    """An outage snapshot is replayed before the healthy snapshot can resolve it."""
    snapshots = iter([_snapshot("postgres_unavailable"), _snapshot()])
    telegram = FakeTelegram()
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: next(snapshots),
    )
    record_snapshot = monitor._record_snapshot
    failed_once = True

    def temporarily_unavailable(reasons: list[str]) -> None:
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise ConnectionError("database unavailable")
        record_snapshot(reasons)

    monitor._record_snapshot = temporarily_unavailable
    asyncio.run(monitor.run_once())
    assert telegram.messages == []
    asyncio.run(monitor.run_once())

    assert [message["text"].split("\n", 1)[0] for message in telegram.messages] == [
        "Execution readiness degraded: postgres_unavailable",
        "Execution readiness recovered: postgres_unavailable",
    ]


def test_alert_configuration_requires_destination_and_token(monkeypatch) -> None:
    monkeypatch.delenv(ALERT_CHAT_ID_ENV_VAR, raising=False)
    monkeypatch.setenv("CODE_AGENT_TELEGRAM_BOT_TOKEN", "token")
    assert readiness_alert_configuration() is None
    monkeypatch.setenv(ALERT_CHAT_ID_ENV_VAR, "not-a-chat")
    assert readiness_alert_configuration() is None
    monkeypatch.setenv(ALERT_CHAT_ID_ENV_VAR, "-123")
    assert readiness_alert_configuration() == ("-123", "token")


def test_readiness_message_is_static_and_sanitized() -> None:
    message = format_readiness_alert("worker_unavailable", "degraded")
    assert message == (
        "Execution readiness degraded: worker_unavailable\n"
        "Next action: Check the Temporal worker and its heartbeat."
    )
    assert "/private/" not in message
    assert "secret" not in message
    assert "task text" not in message


@pytest.mark.parametrize(
    "reason",
    [
        "postgres_unavailable",
        "temporal_unavailable",
        "worker_unavailable",
        "dispatcher_unavailable",
        "dispatcher_backlog_stale",
    ],
)
def test_every_readiness_reason_produces_safe_transition_message(reason: str) -> None:
    message = format_readiness_alert(reason, "degraded")
    assert f"degraded: {reason}" in message
    assert "Next action:" in message
    assert "/private/" not in message


def test_readiness_alert_api_requires_auth_and_exposes_sanitized_status(session_factory) -> None:
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        repository.record_snapshot(["postgres_unavailable"])
        item = repository.claim_due()[0]
        repository.mark_failed(item.id, http_status=502)
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    app = create_app(
        task_service=service,
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )
    with TestClient(app) as client:
        assert client.get("/alerts/readiness").status_code == 401
        headers = {"X-Webhook-Token": DEFAULT_SHARED_SECRET}
        states = client.get("/alerts/readiness", headers=headers)
        deliveries = client.get("/alerts/readiness/deliveries", headers=headers)

    assert states.status_code == 200
    assert states.json()[0]["reason"] == "postgres_unavailable"
    assert deliveries.status_code == 200
    payload = deliveries.json()[0]
    assert payload["last_failure_code"] == "telegram_request_failed"
    assert "bot_token" not in payload
    assert "message" not in payload
