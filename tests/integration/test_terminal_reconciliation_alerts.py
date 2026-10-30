"""Integration coverage for task-specific terminal reconciliation alerts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from apps.api.readiness_alerts import ReadinessAlertMonitor
from db.base import utc_now
from db.enums import OrchestrationRuntime
from db.models import TemporalCommand
from db.readiness_alert_models import ReadinessAlertDelivery, ReadinessAlertState
from orchestrator.execution import TaskExecutionService
from orchestrator.operational_health import (
    TerminalReconciliationAlertSnapshot,
    get_terminal_reconciliation_alert_snapshot,
)
from repositories import (
    ReadinessAlertRepository,
    SessionRepository,
    TaskRepository,
    UserRepository,
    session_scope,
)
from repositories.sqlalchemy_readiness_alerts import TERMINAL_RECONCILIATION_REASON_PREFIX
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


def _monitor(session_factory, telegram: FakeTelegram, probe):
    return ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: SimpleNamespace(degraded_reasons=[], components={}),
        interaction_probe=lambda _service, _at: [],
        outbox_probe=lambda _service, _at: [],
        reconciliation_probe=probe,
    )


def test_complete_task_snapshots_degrade_once_and_recover_each_task(session_factory) -> None:
    observation = {"ids": [f"task-{index:02d}" for index in range(24)]}
    telegram = FakeTelegram()
    monitor = _monitor(
        session_factory,
        telegram,
        lambda _service, _at: list(observation["ids"]),
    )

    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        states = ReadinessAlertRepository(session).list_states()
    task_states = [
        state for state in states if state.reason.startswith(TERMINAL_RECONCILIATION_REASON_PREFIX)
    ]
    assert len(task_states) == 24
    assert {state.transition_number for state in task_states} == {1}
    assert len([item for item in telegram.messages if "degraded" in item["text"]]) == 24

    observation["ids"] = ["task-00", "task-01"]
    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        task_00 = session.get(
            ReadinessAlertState, f"{TERMINAL_RECONCILIATION_REASON_PREFIX}task-00"
        )
        task_02 = session.get(
            ReadinessAlertState, f"{TERMINAL_RECONCILIATION_REASON_PREFIX}task-02"
        )
    assert task_00 is not None and task_00.degraded and task_00.transition_number == 1
    assert task_02 is not None and not task_02.degraded and task_02.transition_number == 2
    assert len([item for item in telegram.messages if "recovered" in item["text"]]) == 22


def test_failed_and_stale_observations_do_not_change_task_alert_state(session_factory) -> None:
    observation = {"fail": False}

    def probe(_service, _at):
        if observation["fail"]:
            raise ConnectionError("Postgres or Temporal observation failed")
        return ["task-stable"]

    monitor = _monitor(session_factory, FakeTelegram(), probe)
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        reason = f"{TERMINAL_RECONCILIATION_REASON_PREFIX}task-stable"
        state = session.get(ReadinessAlertState, reason)
        assert state is not None
        first_checked_at = state.last_checked_at
        assert state.transition_number == 1

    observation["fail"] = True
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        state = session.get(ReadinessAlertState, reason)
        assert state is not None and state.last_checked_at == first_checked_at
        watermark = session.get(
            ReadinessAlertState,
            f"{TERMINAL_RECONCILIATION_REASON_PREFIX}__observation_watermark__",
        )
        assert watermark is not None and watermark.last_checked_at == first_checked_at
        ReadinessAlertRepository(session).record_terminal_reconciliation_snapshot(
            [], observed_at=first_checked_at - timedelta(seconds=1)
        )
        session.flush()
        assert state.degraded is True
        assert state.transition_number == 1


def _pending_reconciliation_start(session_factory, observed_at: datetime):
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="reconciliation-race-user")
        conversation = SessionRepository(session).create(
            user_id=user.id,
            channel="test",
            external_thread_id="reconciliation-race-thread",
        )
        task = TaskRepository(session).create(
            session_id=conversation.id,
            task_text="task start race",
            orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        )
        task_id = task.id
        session.add(
            TemporalCommand(
                task_id=task_id,
                command_type="start",
                sequence_number=1,
                command_key="reconciliation-race-start",
                payload={},
                created_at=observed_at,
                claim_token="active-start-dispatch",
                claim_expires_at=observed_at + timedelta(seconds=20),
            )
        )

    class StartRemainsPending:
        def list_task_workflow_statuses(self) -> dict[str, str]:
            return {task_id: "running"}

    return task_id, SimpleNamespace(
        session_factory=session_factory, temporal_operational_probe=StartRemainsPending()
    )


def test_pending_start_stays_unconfirmed_only_until_grace_elapsed(session_factory) -> None:
    observed_at = utc_now()
    task_id, service = _pending_reconciliation_start(session_factory, observed_at)
    pending_start = get_terminal_reconciliation_alert_snapshot(service, observed_at)
    assert pending_start.divergent_task_ids == ()
    assert pending_start.unconfirmed_task_ids == (task_id,)

    after_grace = get_terminal_reconciliation_alert_snapshot(
        service, observed_at + timedelta(seconds=61)
    )
    assert after_grace.divergent_task_ids == (task_id,)
    assert after_grace.unconfirmed_task_ids == ()


def test_unconfirmed_task_observation_does_not_degrade_or_recover(session_factory) -> None:
    observed_at = utc_now()
    task_id, service = _pending_reconciliation_start(session_factory, observed_at)
    pending_start = get_terminal_reconciliation_alert_snapshot(service, observed_at)

    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        repository.record_terminal_reconciliation_snapshot(
            [task_id], observed_at=observed_at - timedelta(seconds=10)
        )
        state = session.get(
            ReadinessAlertState, f"{TERMINAL_RECONCILIATION_REASON_PREFIX}{task_id}"
        )
        assert state is not None
        prior_checked_at = state.last_checked_at

    telegram = FakeTelegram()
    observation_with_new_task = TerminalReconciliationAlertSnapshot(
        divergent_task_ids=pending_start.divergent_task_ids,
        unconfirmed_task_ids=(*pending_start.unconfirmed_task_ids, "task-never-alerted"),
    )
    monitor = _monitor(session_factory, telegram, lambda _service, _at: observation_with_new_task)
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        state = session.get(
            ReadinessAlertState, f"{TERMINAL_RECONCILIATION_REASON_PREFIX}{task_id}"
        )
        new_task_state = session.get(
            ReadinessAlertState,
            f"{TERMINAL_RECONCILIATION_REASON_PREFIX}task-never-alerted",
        )
    assert state is not None and state.degraded and state.transition_number == 1
    assert state.last_checked_at == prior_checked_at
    assert new_task_state is None
    assert not any("recovered" in item["text"] for item in telegram.messages)


def test_start_ack_after_initial_projection_read_clears_orphan_candidate(session_factory) -> None:
    observed_at = utc_now()
    task_id, _service = _pending_reconciliation_start(session_factory, observed_at)

    class StartAcknowledgedAfterInitialRead:
        def list_task_workflow_statuses(self) -> dict[str, str]:
            with session_scope(session_factory) as session:
                command = session.scalar(
                    select(TemporalCommand).where(TemporalCommand.task_id == task_id)
                )
                assert command is not None
                command.delivered_at = utc_now()
            return {task_id: "running"}

    acknowledged_service = SimpleNamespace(
        session_factory=session_factory,
        temporal_operational_probe=StartAcknowledgedAfterInitialRead(),
    )
    acknowledged = get_terminal_reconciliation_alert_snapshot(acknowledged_service, utc_now())
    assert acknowledged.divergent_task_ids == ()
    assert acknowledged.unconfirmed_task_ids == ()


def test_task_alert_retry_and_authenticated_endpoint_identity(session_factory) -> None:
    telegram = FakeTelegram([503, 200])
    monitor = _monitor(session_factory, telegram, lambda _service, _at: ["task-retry-safe"])
    asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        delivery = session.scalar(
            select(ReadinessAlertDelivery).where(
                ReadinessAlertDelivery.reason
                == f"{TERMINAL_RECONCILIATION_REASON_PREFIX}task-retry-safe"
            )
        )
        assert delivery is not None and delivery.status == "pending"
        delivery.next_attempt_at = utc_now()

    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 2
    assert "Inspect the task and its Temporal workflow." in telegram.messages[0]["text"]
    assert telegram.messages[0]["text"] == telegram.messages[1]["text"]

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

    state = next(item for item in states if item["task_id"] == "task-retry-safe")
    delivery = next(item for item in deliveries if item["task_id"] == "task-retry-safe")
    assert state["reason"] == delivery["reason"] == "terminal_reconciliation_divergent"
    assert delivery["status"] == "delivered"
    assert delivery["attempts"] == 2
