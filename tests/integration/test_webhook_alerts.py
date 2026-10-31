"""Webhook transition delivery and operator inspection regressions."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from apps.api.readiness_alerts import ReadinessAlertMonitor
from apps.api.webhook_alerts import webhook_configuration
from db.base import utc_now
from db.webhook_alert_models import WebhookAlertDelivery
from repositories import ReadinessAlertRepository, session_scope
from repositories.sqlalchemy_webhook_alerts import WebhookAlertRepository
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET
from tests.integration.test_repositories_memory import postgres_session_factory  # noqa: F401


def destination(url="https://example.com/alerts", token="secret"):
    return webhook_configuration(
        {
            "CODE_AGENT_READINESS_ALERT_WEBHOOK_URL": url,
            "CODE_AGENT_READINESS_ALERT_WEBHOOK_TOKEN": token,
        }
    )


def test_outage_retry_recovery_and_inspection(session_factory):
    target = destination()
    seen = []
    statuses = iter([503, 200, 204])
    reasons = ["temporal_unavailable"]

    def sender(_destination, item):
        seen.append(dict(item))
        return next(statuses)

    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=None,
        bot_token="",
        chat_id="",
        webhook_destination=target,
        webhook_sender=sender,
        readiness_probe=lambda _: SimpleNamespace(degraded_reasons=reasons, components={}),
        interaction_probe=lambda *_: [],
        outbox_probe=lambda *_: [],
        reconciliation_probe=lambda *_: [],
    )
    asyncio.run(monitor.run_once())
    reasons.clear()
    asyncio.run(monitor.run_once())
    assert len(seen) == 1
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(WebhookAlertDelivery).order_by(WebhookAlertDelivery.transition_number)
        ).all()
        assert [row.status for row in rows] == ["pending", "pending"]
        rows[0].next_attempt_at = utc_now() - timedelta(seconds=1)
    asyncio.run(monitor.run_once())
    assert [item["transition"] for item in seen] == ["degraded", "degraded", "recovered"]
    assert seen[0]["id"] == seen[1]["id"]
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(WebhookAlertDelivery).order_by(WebhookAlertDelivery.transition_number)
        ).all()
        assert [row.status for row in rows] == ["delivered", "delivered"]
        assert [row.attempts for row in rows] == [2, 1]
        assert ReadinessAlertRepository(session).list_deliveries() == []
    service = SimpleNamespace(session_factory=session_factory)
    app = create_app(
        task_service=service, auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET)
    )
    with TestClient(app) as client:
        assert client.get("/alerts/readiness/deliveries").status_code == 401
        response = client.get(
            "/alerts/readiness/deliveries?channel=webhook",
            headers={"X-Webhook-Token": DEFAULT_SHARED_SECRET},
        )
        assert response.status_code == 200
        assert len(response.json()) == 2
        assert all(
            row["channel"] == "webhook" and row["destination_id"] == target.destination_id
            for row in response.json()
        )
        assert "example.com" not in response.text and "secret" not in response.text


def test_destination_rotation_and_fenced_lease(session_factory):
    first = destination()
    assert first.destination_id == destination(token="rotated").destination_id
    second = destination("https://other.example/alerts")
    with session_scope(session_factory) as session:
        repo = ReadinessAlertRepository(session, webhook_destination_id=first.destination_id)
        repo.record_snapshot(["temporal_unavailable"])
    with session_scope(session_factory) as session:
        repo = WebhookAlertRepository(session)
        assert repo.claim_one(second.destination_id) is None
        item = repo.claim_one(first.destination_id)
        delivery_id, old_owner = item.id, item.lease_owner
        item.lease_expires_at = utc_now() - timedelta(seconds=1)
    with session_scope(session_factory) as session:
        repo = WebhookAlertRepository(session)
        item = repo.claim_one(first.destination_id)
        new_owner = item.lease_owner
        repo.complete(delivery_id, old_owner, 200)
        assert item.status == "processing"
        repo.complete(delivery_id, new_owner, 400)
        assert item.status == "failed"


def test_expired_final_lease_unblocks_recovery(session_factory):
    target = destination()
    with session_scope(session_factory) as session:
        repo = ReadinessAlertRepository(session, webhook_destination_id=target.destination_id)
        now = utc_now()
        repo.record_snapshot(["temporal_unavailable"], observed_at=now)
        repo.record_snapshot([], observed_at=now + timedelta(seconds=1))
    with session_scope(session_factory) as session:
        repo = WebhookAlertRepository(session)
        item = repo.claim_one(target.destination_id)
        item.attempts = 8
        item.lease_expires_at = utc_now() - timedelta(seconds=1)
        delivery_id = item.id
    with session_scope(session_factory) as session:
        repo = WebhookAlertRepository(session)
        assert repo.claim_one(target.destination_id) is None  # recovery is not yet due
        item = session.get(WebhookAlertDelivery, delivery_id)
        assert item.status == "failed"
        recovery = session.scalar(
            select(WebhookAlertDelivery).where(WebhookAlertDelivery.transition_number == 2)
        )
        recovery.next_attempt_at = utc_now() - timedelta(seconds=1)
    with session_scope(session_factory) as session:
        assert (
            WebhookAlertRepository(session).claim_one(target.destination_id).transition
            == "recovered"
        )


@pytest.mark.parametrize(
    "status, expected",
    [
        (200, "delivered"),
        (299, "delivered"),
        (302, "failed"),
        (400, "failed"),
        (408, "pending"),
        (429, "pending"),
        (500, "pending"),
        (599, "pending"),
        (None, "pending"),
    ],
)
def test_response_classification(session_factory, status, expected):
    target = destination()
    with session_scope(session_factory) as session:
        ReadinessAlertRepository(
            session, webhook_destination_id=target.destination_id
        ).record_snapshot(["temporal_unavailable"])
    with session_scope(session_factory) as session:
        repo = WebhookAlertRepository(session)
        item = repo.claim_one(target.destination_id)
        repo.complete(item.id, item.lease_owner, status)
        assert item.status == expected


def test_transition_enqueue_rolls_back_atomically(session_factory):
    target = destination()
    with pytest.raises(RuntimeError), session_scope(session_factory) as session:
        ReadinessAlertRepository(
            session, webhook_destination_id=target.destination_id
        ).record_snapshot(["temporal_unavailable"])
        raise RuntimeError("rollback")
    with session_scope(session_factory) as session:
        assert session.scalars(select(WebhookAlertDelivery)).all() == []
        assert ReadinessAlertRepository(session).list_deliveries() == []


def test_postgres_concurrent_claims(postgres_session_factory):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    target = destination()
    with session_scope(postgres_session_factory) as session:
        ReadinessAlertRepository(
            session, webhook_destination_id=target.destination_id
        ).record_snapshot(["temporal_unavailable"])
    barrier = Barrier(2)

    def claim(_):
        with session_scope(postgres_session_factory) as session:
            barrier.wait()
            item = WebhookAlertRepository(session).claim_one(target.destination_id)
            return item.id if item else None

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, range(2)))
    assert len([value for value in claims if value]) == 1


def test_no_historical_backfill_and_destination_changes(session_factory):
    first, second = destination(), destination("https://second.example/")
    with session_scope(session_factory) as session:
        ReadinessAlertRepository(session).record_snapshot(["temporal_unavailable"])
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session, webhook_destination_id=first.destination_id)
        repository.record_snapshot(
            ["temporal_unavailable"], observed_at=utc_now() + timedelta(seconds=1)
        )
        assert session.scalars(select(WebhookAlertDelivery)).all() == []
        repository.record_snapshot([], observed_at=utc_now() + timedelta(seconds=2))
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session, webhook_destination_id=second.destination_id)
        repository.record_snapshot(
            ["temporal_unavailable"], observed_at=utc_now() + timedelta(seconds=3)
        )
    with session_scope(session_factory) as session:
        items = session.scalars(
            select(WebhookAlertDelivery).order_by(WebhookAlertDelivery.transition_number)
        ).all()
        assert [(item.transition, item.destination_id) for item in items] == [
            ("recovered", first.destination_id),
            ("degraded", second.destination_id),
        ]


def test_telegram_failure_does_not_block_webhook(session_factory, monkeypatch, caplog):
    from tests.integration.test_readiness_alerts import FakeTelegram

    sent = []
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=FakeTelegram(),
        bot_token="token",
        chat_id="1",
        webhook_destination=destination(),
        webhook_sender=lambda _, item: sent.append(item) or 200,
        readiness_probe=lambda _: SimpleNamespace(
            degraded_reasons=["worker_unavailable"], components={}
        ),
        interaction_probe=lambda *_: [],
        outbox_probe=lambda *_: [],
        reconciliation_probe=lambda *_: [],
    )

    def fail():
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(monitor, "_claim_one", fail)
    asyncio.run(monitor.run_once())
    assert len(sent) == 1
    assert "private diagnostic" not in caplog.text


def test_slow_completion_does_not_block_event_loop(session_factory, monkeypatch):
    from threading import Event

    from repositories.sqlalchemy_webhook_alerts import WebhookAlertRepository

    entered, release = Event(), Event()
    original = WebhookAlertRepository.complete

    def slow_complete(self, *args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WebhookAlertRepository, "complete", slow_complete)
    target = destination()
    with session_scope(session_factory) as session:
        ReadinessAlertRepository(
            session, webhook_destination_id=target.destination_id
        ).record_snapshot(["worker_unavailable"])
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=None,
        bot_token="",
        chat_id="",
        webhook_destination=target,
        webhook_sender=lambda *_: 200,
    )
    item = monitor._claim_webhook()

    async def exercise():
        task = asyncio.create_task(monitor._deliver_webhook(item))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.5)
            assert not task.done()
        finally:
            release.set()
            await task

    asyncio.run(exercise())
