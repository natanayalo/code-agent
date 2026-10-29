"""Integration coverage for task-specific stuck-interaction alerts."""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from apps.api.readiness_alerts import ReadinessAlertMonitor
from db.base import utc_now
from db.readiness_alert_models import ReadinessAlertDelivery, ReadinessAlertState
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


def _monitor(session_factory, telegram: FakeTelegram, interaction_probe):
    return ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: SimpleNamespace(degraded_reasons=[], components={}),
        interaction_probe=interaction_probe,
    )


def test_interaction_alerts_track_complete_task_set_and_failed_observations(
    session_factory,
) -> None:
    task_ids = [f"task-{index:02d}" for index in range(24)]
    observation = {"ids": task_ids}
    observed_at = []
    telegram = FakeTelegram()
    monitor = _monitor(
        session_factory,
        telegram,
        lambda _service, checked_at: observed_at.append(checked_at) or list(observation["ids"]),
    )

    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 20
    with session_scope(session_factory) as session:
        assert len(ReadinessAlertRepository(session).list_states()) == 24
        first_state = session.get(ReadinessAlertState, "interaction_wait_stuck|task-00")
        assert first_state is not None
        transition_at = first_state.last_transition_at
        if transition_at.tzinfo is None:
            transition_at = transition_at.replace(tzinfo=UTC)
        assert transition_at == observed_at[0]
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 24
    assert all("interaction_wait_stuck" in item["text"] for item in telegram.messages)
    assert all("Task:" in item["text"] for item in telegram.messages)
    assert {
        item["text"].split("Task: ", 1)[1].split("\n", 1)[0] for item in telegram.messages
    } == set(task_ids)

    observation["ids"] = task_ids[:2]
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 44
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 46
    assert len([item for item in telegram.messages if "recovered" in item["text"]]) == 22

    def fail_observation(_service, _observed_at):
        raise RuntimeError("observation unavailable")

    monitor.interaction_probe = fail_observation
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 46
    with session_scope(session_factory) as session:
        state = session.get(ReadinessAlertState, "interaction_wait_stuck|task-00")
        assert state is not None and state.degraded is True
        assert state.transition_number == 1


def test_interaction_alert_api_exposes_sanitized_task_identity(session_factory) -> None:
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        repository.record_interaction_snapshot(["task-safe-id"])
        item = repository.claim_due()[0]
        repository.mark_failed(item.id, http_status=502)
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    app = create_app(
        task_service=service, auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET)
    )
    with TestClient(app) as client:
        assert client.get("/alerts/readiness").status_code == 401
        headers = {"X-Webhook-Token": DEFAULT_SHARED_SECRET}
        states = client.get("/alerts/readiness", headers=headers)
        deliveries = client.get("/alerts/readiness/deliveries", headers=headers)

    state = next(item for item in states.json() if item["task_id"] == "task-safe-id")
    delivery = next(item for item in deliveries.json() if item["task_id"] == "task-safe-id")
    assert state["reason"] == delivery["reason"] == "interaction_wait_stuck"
    assert delivery["last_failure_code"] == "telegram_request_failed"
    assert "message" not in delivery


def test_interaction_alert_delivery_retries_through_existing_queue(session_factory) -> None:
    telegram = FakeTelegram([503, 200])
    monitor = _monitor(session_factory, telegram, lambda _service, _observed_at: ["task-retry"])
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        delivery = session.scalar(
            select(ReadinessAlertDelivery).where(
                ReadinessAlertDelivery.reason == "interaction_wait_stuck|task-retry"
            )
        )
        assert delivery is not None
        assert delivery.status == "pending"
        assert delivery.attempts == 1
        delivery.next_attempt_at = utc_now()

    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        deliveries = ReadinessAlertRepository(session).list_deliveries()
    assert len(deliveries) == 1
    assert deliveries[0].status == "delivered"
    assert deliveries[0].attempts == 2
    assert len(telegram.messages) == 2
    assert all("task-retry" in message["text"] for message in telegram.messages)


def test_stale_interaction_observation_cannot_override_newer_empty_set(session_factory) -> None:
    now = utc_now()
    newer_at = now - timedelta(seconds=1)
    stale_at = newer_at - timedelta(seconds=1)
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        repository.record_interaction_snapshot([], observed_at=newer_at)
        repository.record_interaction_snapshot(["task-stale"], observed_at=stale_at)
        state = session.get(ReadinessAlertState, "interaction_wait_stuck|task-stale")
        deliveries = repository.list_deliveries()

    assert state is None
    assert deliveries == []
