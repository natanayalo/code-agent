"""Exhausted leases must release later transitions without another send."""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from apps.api.readiness_alerts import ReadinessAlertMonitor
from db.base import utc_now
from db.readiness_alert_models import ReadinessAlertDelivery
from repositories import ReadinessAlertRepository, session_scope
from tests.integration.test_readiness_alerts import FakeTelegram, _snapshot


@pytest.mark.parametrize("status", ["processing", "pending"])
@pytest.mark.parametrize("attempts", [8, 9])
def test_exhausted_expired_delivery_unblocks_recovery(session_factory, status, attempts):
    with session_scope(session_factory) as session:
        repo = ReadinessAlertRepository(session)
        repo.record_snapshot(["worker_unavailable"])
        item = repo.claim_due(limit=1)[0]
        item.status = status
        item.attempts = attempts
        item.lease_expires_at = utc_now() - timedelta(seconds=1)
        item.next_attempt_at = utc_now() - timedelta(seconds=1)
        item_id = item.id
        repo.record_snapshot([])
    telegram = FakeTelegram()
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="synthetic",
        chat_id="42",
        readiness_probe=lambda _: _snapshot(),
        interaction_probe=lambda *_: [],
        outbox_probe=lambda *_: [],
        reconciliation_probe=lambda *_: [],
    )
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        item = session.get(ReadinessAlertDelivery, item_id)
        assert item.status == "failed"
        assert item.attempts == attempts
        assert item.lease_expires_at is None
        assert item.last_failure_code == "telegram_attempts_exhausted"
    assert len(telegram.messages) == 1
    assert "recovered" in telegram.messages[0]["text"]


def test_compose_passes_readiness_webhook_configuration():
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )
    environment = compose["services"]["api"]["environment"]
    for key in (
        "CODE_AGENT_READINESS_ALERT_WEBHOOK_URL",
        "CODE_AGENT_READINESS_ALERT_WEBHOOK_TOKEN",
    ):
        assert environment[key] == "${" + key + ":-}"
