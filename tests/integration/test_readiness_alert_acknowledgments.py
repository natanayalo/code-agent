"""Operator acknowledgment behavior and delivery race coverage."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.readiness_alert_models import (
    ReadinessAlertAcknowledgment,
    ReadinessAlertDelivery,
)
from db.webhook_alert_models import WebhookAlertDelivery
from orchestrator.execution import TaskExecutionService
from repositories import ReadinessAlertRepository, session_scope
from repositories.sqlalchemy_webhook_alerts import WebhookAlertRepository
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET, _default_worker


def _app(session_factory):
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    return create_app(
        task_service=service,
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )


def _headers() -> dict[str, str]:
    return {"X-Webhook-Token": DEFAULT_SHARED_SECRET}


def test_acknowledgment_auth_idempotency_suppression_and_recurrence(session_factory) -> None:
    with session_scope(session_factory) as session:
        ReadinessAlertRepository(session, webhook_destination_id="destination").record_snapshot(
            ["postgres_unavailable"]
        )
    app = _app(session_factory)
    payload = {"reason": "postgres_unavailable", "transition_number": 1}
    with TestClient(app) as client:
        assert client.post("/alerts/readiness/acknowledgments", json=payload).status_code == 401
        acknowledged = client.post(
            "/alerts/readiness/acknowledgments", json=payload, headers=_headers()
        )
        repeated = client.post(
            "/alerts/readiness/acknowledgments", json=payload, headers=_headers()
        )
        states = client.get("/alerts/readiness", headers=_headers())

    assert acknowledged.status_code == 200
    assert repeated.json() == acknowledged.json()
    assert acknowledged.json()["acknowledged_by"] == "operator"
    state = next(item for item in states.json() if item["reason"] == "postgres_unavailable")
    assert state["degraded"] is True
    assert state["acknowledged_at"] == acknowledged.json()["acknowledged_at"]
    with session_scope(session_factory) as session:
        telegram = session.scalars(select(ReadinessAlertDelivery)).all()
        webhook = session.scalars(select(WebhookAlertDelivery)).all()
        audit = session.scalars(select(ReadinessAlertAcknowledgment)).all()
        repository = ReadinessAlertRepository(session, webhook_destination_id="destination")
        repository.record_snapshot([])
        repository.record_snapshot(["postgres_unavailable"])
        history = repository.list_deliveries(limit=10)
    with TestClient(app) as client:
        recurrent = client.get("/alerts/readiness", headers=_headers())

    assert [item.status for item in telegram] == ["suppressed"]
    assert [item.status for item in webhook] == ["suppressed"]
    assert len(audit) == 1
    assert any(item.transition == "recovered" and item.status == "pending" for item in history)
    recurrent_state = next(
        item for item in recurrent.json() if item["reason"] == "postgres_unavailable"
    )
    assert recurrent_state["transition_number"] == 3
    assert recurrent_state["acknowledged_at"] is None


def test_stale_unacknowledged_request_is_rejected(session_factory) -> None:
    app = _app(session_factory)
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        repository.record_snapshot(["worker_unavailable"])
        repository.record_snapshot([])
    with TestClient(app) as client:
        stale = client.post(
            "/alerts/readiness/acknowledgments",
            json={"reason": "worker_unavailable", "transition_number": 1},
            headers=_headers(),
        )
    assert stale.status_code == 409


def test_task_specific_public_reason_acknowledges_its_stored_incident_key(session_factory) -> None:
    app = _app(session_factory)
    with session_scope(session_factory) as session:
        ReadinessAlertRepository(session).record_interaction_snapshot(["task-42"])
    with TestClient(app) as client:
        states = client.get("/alerts/readiness", headers=_headers())
        incident = next(
            item
            for item in states.json()
            if item["reason"] == "interaction_wait_stuck" and item["task_id"] == "task-42"
        )
        acknowledged = client.post(
            "/alerts/readiness/acknowledgments",
            json={
                "reason": incident["acknowledgment_reason"],
                "transition_number": incident["transition_number"],
            },
            headers=_headers(),
        )
    assert incident["acknowledgment_reason"] == "interaction_wait_stuck|task-42"
    assert acknowledged.status_code == 200


def test_acknowledgment_leaves_both_in_flight_deliveries_completable(session_factory) -> None:
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session, webhook_destination_id="destination")
        repository.record_snapshot(["temporal_unavailable"])
        telegram = repository.claim_due(limit=1)[0]
        webhook = WebhookAlertRepository(session).claim_one("destination")
        assert webhook is not None
        repository.acknowledge_incident("temporal_unavailable", 1, actor="operator")
        repository.mark_delivered(telegram.id, http_status=200)
        WebhookAlertRepository(session).complete(webhook.id, webhook.lease_owner, 200)
        telegram_status = session.get(ReadinessAlertDelivery, telegram.id).status
        webhook_status = session.get(WebhookAlertDelivery, webhook.id).status
    assert telegram_status == webhook_status == "delivered"


def test_acknowledgment_suppresses_retries_when_in_flight_deliveries_fail(session_factory) -> None:
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session, webhook_destination_id="destination")
        repository.record_snapshot(["temporal_unavailable"])
        telegram = repository.claim_due(limit=1)[0]
        webhook = WebhookAlertRepository(session).claim_one("destination")
        assert webhook is not None
        repository.acknowledge_incident("temporal_unavailable", 1, actor="operator")
        repository.mark_failed(telegram.id, http_status=503)
        WebhookAlertRepository(session).complete(webhook.id, webhook.lease_owner, 503)
        telegram_row = session.get(ReadinessAlertDelivery, telegram.id)
        webhook_row = session.get(WebhookAlertDelivery, webhook.id)
    assert telegram_row is not None and telegram_row.status == "suppressed"
    assert webhook_row is not None and webhook_row.status == "suppressed"
    assert telegram_row.last_failure_code == "telegram_request_failed"
    assert webhook_row.last_failure_code == "webhook_request_failed"
