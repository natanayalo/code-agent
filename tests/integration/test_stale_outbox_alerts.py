"""Integration coverage for task-specific stale Temporal outbox alerts."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from apps.api.readiness_alerts import ReadinessAlertMonitor, format_readiness_alert
from db.base import utc_now
from db.models import TemporalCommand
from db.readiness_alert_models import ReadinessAlertDelivery, ReadinessAlertState
from orchestrator.execution import TaskExecutionService
from repositories import (
    ReadinessAlertRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    session_scope,
)
from repositories.sqlalchemy_readiness_alerts import OUTBOX_STALE_REASON_PREFIX
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET, _default_worker


class FakeTelegramResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("HTTP failure")

    def json(self) -> dict[str, bool]:
        return {"ok": self.status_code < 400}


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def post(self, url: str, *, json: dict, timeout: int) -> FakeTelegramResponse:
        self.messages.append({"url": url, **json})
        return FakeTelegramResponse(200)


def _create_tasks_with_commands(session_factory, count: int):
    now = utc_now()
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="outbox-alert-user")
        conversation = SessionRepository(session).create(
            user_id=user.id,
            channel="test",
            external_thread_id="outbox-alert-thread",
        )
        task_repo = TaskRepository(session)
        tasks = [
            task_repo.create(session_id=conversation.id, task_text=f"task {index}")
            for index in range(count)
        ]
        for index, task in enumerate(tasks):
            for command_number in range(2 if index == 0 else 1):
                session.add(
                    TemporalCommand(
                        task_id=task.id,
                        command_type="start" if command_number == 0 else "signal",
                        sequence_number=command_number + 1,
                        command_key=f"outbox-alert-{index}-{command_number}",
                        payload={},
                        created_at=now - timedelta(seconds=61),
                        next_attempt_at=now - timedelta(seconds=1),
                    )
                )
        return [task.id for task in tasks]


def _monitor(session_factory, telegram, *, outbox_probe=None):
    return ReadinessAlertMonitor(
        task_service=SimpleNamespace(session_factory=session_factory),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: SimpleNamespace(degraded_reasons=[], components={}),
        interaction_probe=lambda _service, _observed_at: [],
        outbox_probe=outbox_probe,
    )


def test_complete_stale_task_set_transitions_once_and_maps_public_identity(session_factory) -> None:
    task_ids = _create_tasks_with_commands(session_factory, 24)
    telegram = FakeTelegram()
    monitor = _monitor(session_factory, telegram)

    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 24
    assert len([m for m in telegram.messages if "degraded" in m["text"]]) == 24
    assert all("outbox_command_stale" in m["text"] for m in telegram.messages)
    assert all(
        "Inspect the Temporal command outbox and worker dispatcher." in m["text"]
        for m in telegram.messages
    )
    assert "task text" not in " ".join(m["text"] for m in telegram.messages)

    with session_scope(session_factory) as session:
        states = ReadinessAlertRepository(session).list_states()
        assert len([s for s in states if s.reason.startswith(OUTBOX_STALE_REASON_PREFIX)]) == 24
        commands = session.query(TemporalCommand).all()
        for command in commands:
            command.delivered_at = utc_now()

    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 48
    assert len([m for m in telegram.messages if "recovered" in m["text"]]) == 24

    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    app = create_app(
        task_service=service,
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )
    with TestClient(app) as client:
        assert client.get("/alerts/readiness").status_code == 401
        headers = {"X-Webhook-Token": DEFAULT_SHARED_SECRET}
        states = client.get("/alerts/readiness", headers=headers).json()
        deliveries = client.get("/alerts/readiness/deliveries", headers=headers).json()

    task_state = next(item for item in states if item["task_id"] == task_ids[0])
    task_delivery = next(item for item in deliveries if item["task_id"] == task_ids[0])
    assert task_state["reason"] == task_delivery["reason"] == "outbox_command_stale"


def test_failed_and_out_of_order_stale_observations_leave_state_unchanged(session_factory) -> None:
    observed = {"fail": False, "ids": ["task-stale"]}

    def probe(_service, _at):
        if observed["fail"]:
            raise RuntimeError("outbox unavailable")
        return observed["ids"]

    monitor = _monitor(session_factory, FakeTelegram(), outbox_probe=probe)
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        state = session.get(ReadinessAlertState, f"{OUTBOX_STALE_REASON_PREFIX}task-stale")
        assert state is not None and state.degraded
        first_observation = state.last_checked_at
        transition_number = state.transition_number

    observed["fail"] = True
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        state = session.get(ReadinessAlertState, f"{OUTBOX_STALE_REASON_PREFIX}task-stale")
        assert state is not None
        assert state.last_checked_at == first_observation
        assert state.transition_number == transition_number
    monitor._record_snapshots([([], [], first_observation + timedelta(seconds=1))])
    with session_scope(session_factory) as session:
        state = session.get(ReadinessAlertState, f"{OUTBOX_STALE_REASON_PREFIX}task-stale")
        assert state is not None and state.degraded
        ReadinessAlertRepository(session).record_stale_outbox_snapshot(
            [], observed_at=first_observation - timedelta(seconds=1)
        )
        session.flush()
        assert state.degraded is True
        assert state.transition_number == transition_number


def test_concurrent_monitors_and_task_delivery_ordering(session_factory) -> None:
    task_ids = _create_tasks_with_commands(session_factory, 1)
    telegram = FakeTelegram()
    monitors = [_monitor(session_factory, telegram), _monitor(session_factory, telegram)]

    async def run_both():
        await asyncio.gather(*(monitor.run_once() for monitor in monitors))

    asyncio.run(run_both())
    with session_scope(session_factory) as session:
        rows = (
            session.query(ReadinessAlertDelivery)
            .filter(ReadinessAlertDelivery.reason == f"{OUTBOX_STALE_REASON_PREFIX}{task_ids[0]}")
            .all()
        )
        assert [(row.transition_number, row.transition) for row in rows] == [(1, "degraded")]
        for command in session.query(TemporalCommand).all():
            command.delivered_at = utc_now()

    asyncio.run(run_both())
    messages = [item["text"] for item in telegram.messages]
    assert ["degraded" in message for message in messages] == [True, False]
    formatted = format_readiness_alert(f"{OUTBOX_STALE_REASON_PREFIX}{task_ids[0]}", "recovered")
    assert formatted.startswith("Execution alert recovered: outbox_command_stale")

    with session_scope(session_factory) as session:
        rows = (
            session.query(ReadinessAlertDelivery)
            .filter(ReadinessAlertDelivery.reason == f"{OUTBOX_STALE_REASON_PREFIX}{task_ids[0]}")
            .order_by(ReadinessAlertDelivery.transition_number)
            .all()
        )
        assert [(row.transition_number, row.transition) for row in rows] == [
            (1, "degraded"),
            (2, "recovered"),
        ]
