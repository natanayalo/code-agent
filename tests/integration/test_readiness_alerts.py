"""Integration coverage for readiness alert transitions and operator inspection."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from apps.api.readiness_alerts import (
    ALERT_CHAT_ID_ENV_VAR,
    ReadinessAlertMonitor,
    format_readiness_alert,
    readiness_alert_configuration,
)
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


class SlowTelegram(FakeTelegram):
    async def post(self, url: str, *, json: dict, timeout: int) -> FakeResponse:
        await asyncio.sleep(0.3)
        return await super().post(url, json=json, timeout=timeout)


def _snapshot(*reasons: str, components: dict | None = None):
    return SimpleNamespace(degraded_reasons=list(reasons), components=components or {})


def _delivery_rows(session_factory) -> list[ReadinessAlertDelivery]:
    with session_scope(session_factory) as session:
        return ReadinessAlertRepository(session).list_deliveries()


def _seed_delivered_incident(session_factory, reason: str, degraded_at, recovered_at) -> None:
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        repository.record_snapshot([reason], observed_at=degraded_at)
        repository.record_snapshot([], observed_at=recovered_at)
        for _ in range(2):
            for delivery in repository.claim_due(limit=10):
                repository.mark_delivered(delivery.id, http_status=200)


def _incident_replay_samples(times: list[datetime], reason: str) -> list[SimpleNamespace]:
    degraded_at_1, recovered_at_1, degraded_at_2, recovered_at_2, checked_at = times
    return [
        SimpleNamespace(degraded_reasons=[reason], components={}, checked_at=degraded_at_1),
        SimpleNamespace(degraded_reasons=[], components={}, checked_at=recovered_at_1),
        SimpleNamespace(degraded_reasons=[reason], components={}, checked_at=degraded_at_2),
        SimpleNamespace(degraded_reasons=[], components={}, checked_at=recovered_at_2),
        SimpleNamespace(degraded_reasons=[], components={}, checked_at=checked_at),
    ]


def _transition_history(session_factory) -> list[tuple[str, int, str, str]]:
    return sorted(
        (
            item.reason,
            item.transition_number,
            item.transition,
            item.status,
        )
        for item in _delivery_rows(session_factory)
    )


def _assert_replayed_incidents(
    session_factory, stale_telegram: FakeTelegram, current_telegram: FakeTelegram
) -> None:
    expected_messages = [
        "Execution readiness degraded: temporal_unavailable",
        "Execution readiness recovered: temporal_unavailable",
        "Execution readiness degraded: temporal_unavailable",
        "Execution readiness recovered: temporal_unavailable",
    ]
    assert [message["text"].split("\n", 1)[0] for message in stale_telegram.messages] == (
        expected_messages
    )
    assert current_telegram.messages == []
    assert _transition_history(session_factory) == [
        ("temporal_unavailable", number, transition, "delivered")
        for number, transition in (
            (1, "degraded"),
            (2, "recovered"),
            (3, "degraded"),
            (4, "recovered"),
            (5, "degraded"),
            (6, "recovered"),
        )
    ]


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


def test_stale_readiness_observation_cannot_override_newer_healthy_sample(session_factory) -> None:
    newer_at = utc_now() - timedelta(seconds=1)
    stale_at = newer_at - timedelta(seconds=1)
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        repository.record_snapshot([], observed_at=newer_at)
        repository.record_snapshot(["worker_unavailable"], observed_at=stale_at)
        state = session.get(ReadinessAlertState, "worker_unavailable")
        deliveries = repository.list_deliveries()

    assert state is not None
    assert state.degraded is False
    checked_at = state.last_checked_at
    assert (checked_at.replace(tzinfo=UTC) if checked_at.tzinfo is None else checked_at) == newer_at
    assert all(item.reason != "worker_unavailable" for item in deliveries)


def test_completed_buffered_incident_replays_before_initial_healthy_baseline(
    session_factory,
) -> None:
    now = utc_now()
    degraded_at = now - timedelta(seconds=3)
    recovered_at = now - timedelta(seconds=2)
    healthy_at = now - timedelta(seconds=1)
    with session_scope(session_factory) as session:
        ReadinessAlertRepository(session).record_snapshot([], observed_at=healthy_at)

    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=FakeTelegram(),
        bot_token="token",
        chat_id="123",
    )
    monitor._record_snapshots(
        [
            (["worker_unavailable"], [], degraded_at),
            ([], [], recovered_at),
        ]
    )

    with session_scope(session_factory) as session:
        state = session.get(ReadinessAlertState, "worker_unavailable")
        deliveries = ReadinessAlertRepository(session).list_deliveries()
    assert state is not None
    assert not state.degraded
    assert state.last_checked_at.replace(tzinfo=UTC) == healthy_at
    assert state.last_transition_at.replace(tzinfo=UTC) == healthy_at
    assert state.transition_number == 2
    assert sorted(
        (
            item.transition_number,
            item.transition,
            item.observed_at.replace(tzinfo=UTC),
        )
        for item in deliveries
    ) == [
        (1, "degraded", degraded_at),
        (2, "recovered", recovered_at),
    ]


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


def test_recovery_waits_for_degradation_retry(session_factory) -> None:
    """A due recovery cannot overtake an earlier transition waiting for retry."""
    reasons = ["worker_unavailable"]
    telegram = FakeTelegram([503])
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: _snapshot(*reasons),
    )
    asyncio.run(monitor.run_once())
    reasons.clear()
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 1

    with session_scope(session_factory) as session:
        degraded = session.scalar(
            select(ReadinessAlertDelivery).where(ReadinessAlertDelivery.transition == "degraded")
        )
        degraded.next_attempt_at = utc_now() - timedelta(seconds=1)
    asyncio.run(monitor.run_once())

    assert [message["text"].split("\n", 1)[0] for message in telegram.messages] == [
        "Execution readiness degraded: worker_unavailable",
        "Execution readiness degraded: worker_unavailable",
        "Execution readiness recovered: worker_unavailable",
    ]


def test_exhausted_transition_does_not_block_later_transition(session_factory) -> None:
    """A terminal failed delivery remains visible but releases the reason queue."""
    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        repository.record_snapshot(["worker_unavailable"])
        degraded = repository.claim_due(limit=1)[0]
        degraded.attempts = 8
        repository.mark_failed(degraded.id, http_status=503)
        repository.record_snapshot([])
        recovery = repository.claim_due(limit=1)

    assert degraded.status == "failed"
    assert len(recovery) == 1
    assert recovery[0].transition == "recovered"


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


def test_slow_batch_does_not_expire_leases_for_queued_deliveries(
    session_factory, monkeypatch
) -> None:
    """Instances claim one item at a time so a slow batch cannot lease stale work."""
    from repositories import sqlalchemy_readiness_alerts

    monkeypatch.setattr(sqlalchemy_readiness_alerts, "DELIVERY_LEASE_SECONDS", 1)
    reasons = [
        "postgres_unavailable",
        "temporal_unavailable",
        "worker_unavailable",
        "dispatcher_unavailable",
        "dispatcher_backlog_stale",
    ]
    telegram = SlowTelegram()
    monitors = [
        ReadinessAlertMonitor(
            task_service=object(),
            session_factory=session_factory,
            telegram_client=telegram,
            bot_token="token",
            chat_id="123",
            readiness_probe=lambda _service: _snapshot(*reasons),
        )
        for _ in range(2)
    ]

    async def run_with_overlap() -> None:
        first = asyncio.create_task(monitors[0].run_once())
        await asyncio.sleep(1.1)
        second = asyncio.create_task(monitors[1].run_once())
        await asyncio.gather(first, second)

    asyncio.run(run_with_overlap())
    assert len(telegram.messages) == len(reasons)
    deliveries = _delivery_rows(session_factory)
    assert all(delivery.status == "delivered" for delivery in deliveries)


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
    record_snapshots = monitor._record_snapshots
    failed_once = True

    def temporarily_unavailable(samples: list[tuple[list[str], list[str], datetime]]) -> None:
        nonlocal failed_once
        if failed_once:
            failed_once = False
            raise ConnectionError("database unavailable")
        record_snapshots(samples)

    monitor._record_snapshots = temporarily_unavailable
    asyncio.run(monitor.run_once())
    assert telegram.messages == []
    asyncio.run(monitor.run_once())

    assert [message["text"].split("\n", 1)[0] for message in telegram.messages] == [
        "Execution readiness degraded: postgres_unavailable",
        "Execution readiness recovered: postgres_unavailable",
    ]


def test_unknown_worker_observation_preserves_degraded_state_during_postgres_outage(
    session_factory,
) -> None:
    """Postgres recovery does not emit false worker recovery if the worker stays down."""
    ready = SimpleNamespace(status="ready")
    unavailable = SimpleNamespace(status="not_ready")
    unknown = SimpleNamespace(status="unknown")
    snapshots = iter(
        [
            _snapshot(
                "worker_unavailable",
                components={
                    "postgres": ready,
                    "temporal": ready,
                    "worker": unavailable,
                    "dispatcher": ready,
                },
            ),
            _snapshot(
                "postgres_unavailable",
                components={
                    "postgres": unavailable,
                    "temporal": ready,
                    "worker": unknown,
                    "dispatcher": unknown,
                },
            ),
            _snapshot(
                "worker_unavailable",
                components={
                    "postgres": ready,
                    "temporal": ready,
                    "worker": unavailable,
                    "dispatcher": ready,
                },
            ),
        ]
    )
    telegram = FakeTelegram()
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: next(snapshots),
    )
    record_snapshots = monitor._record_snapshots
    calls = 0

    def fail_postgres_sample_once(samples: list[tuple[list[str], list[str], datetime]]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("database unavailable")
        record_snapshots(samples)

    monitor._record_snapshots = fail_postgres_sample_once
    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())

    assert [message["text"].split("\n", 1)[0] for message in telegram.messages] == [
        "Execution readiness degraded: worker_unavailable",
        "Execution readiness degraded: postgres_unavailable",
        "Execution readiness recovered: postgres_unavailable",
    ]


def test_stale_worker_replay_restores_newer_healthy_state_after_unknown_sample(
    session_factory,
) -> None:
    """An older buffered outage cannot overwrite a newer known healthy state."""
    checked_at = utc_now()
    degraded_at = checked_at - timedelta(seconds=3)
    healthy_at = checked_at - timedelta(seconds=2)
    unknown_at = checked_at - timedelta(seconds=1)
    with session_scope(session_factory) as session:
        ReadinessAlertRepository(session).record_snapshot([], observed_at=healthy_at)

    unknown = SimpleNamespace(status="unknown")
    unavailable = SimpleNamespace(status="not_ready")
    samples = iter(
        [
            SimpleNamespace(
                degraded_reasons=["worker_unavailable"],
                components={"worker": unavailable},
                checked_at=degraded_at,
            ),
            SimpleNamespace(
                degraded_reasons=["postgres_unavailable"],
                components={"postgres": unavailable, "worker": unknown, "dispatcher": unknown},
                checked_at=unknown_at,
            ),
        ]
    )
    telegram = FakeTelegram()
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: next(samples),
    )
    persist = monitor._record_snapshots
    fail_once = True

    def fail_first_replay(
        buffered: list[tuple[list[str], list[str], datetime]],
    ) -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise ConnectionError("database unavailable")
        persist(buffered)

    monitor._record_snapshots = fail_first_replay
    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())

    with session_scope(session_factory) as session:
        worker_state = session.get(ReadinessAlertState, "worker_unavailable")
        assert worker_state is not None
        assert worker_state.degraded is False
        worker_deliveries = session.scalars(
            select(ReadinessAlertDelivery)
            .where(ReadinessAlertDelivery.reason == "worker_unavailable")
            .order_by(ReadinessAlertDelivery.transition_number)
        ).all()
        assert worker_deliveries == []

    assert sorted(message["text"].split("\n", 1)[0] for message in telegram.messages) == sorted(
        ["Execution readiness degraded: postgres_unavailable"]
    )


def test_buffered_replay_rolls_back_atomically_after_partial_failure(
    session_factory, monkeypatch
) -> None:
    """A failed replay transaction retains the whole buffer without partial transitions."""
    snapshots = iter([_snapshot("worker_unavailable"), _snapshot(), _snapshot()])
    telegram = FakeTelegram()
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: next(snapshots),
    )
    original_record_snapshot = ReadinessAlertRepository.record_snapshot
    calls = 0

    def fail_inside_replay_once(
        repository: ReadinessAlertRepository, reasons: list[str], **kwargs: Any
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("database unavailable")
        original_record_snapshot(repository, reasons, **kwargs)
        if calls == 3:
            raise ConnectionError("database unavailable")

    monkeypatch.setattr(ReadinessAlertRepository, "record_snapshot", fail_inside_replay_once)
    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())
    assert _delivery_rows(session_factory) == []
    asyncio.run(monitor.run_once())

    assert sorted(
        (item.reason, item.transition, item.status) for item in _delivery_rows(session_factory)
    ) == [
        ("worker_unavailable", "degraded", "delivered"),
        ("worker_unavailable", "recovered", "delivered"),
    ]


def test_multiple_unreported_outages_replay_after_newer_healthy_observation(
    session_factory,
) -> None:
    """Buffered outages replay individually even after earlier transition history."""
    stale_telegram = FakeTelegram()
    current_telegram = FakeTelegram()
    checked_at = utc_now()
    previous_outage_at = checked_at - timedelta(seconds=20)
    previous_recovery_at = checked_at - timedelta(seconds=18)
    first_outage_at = checked_at - timedelta(seconds=12)
    first_recovery_at = checked_at - timedelta(seconds=10)
    second_outage_at = checked_at - timedelta(seconds=8)
    second_recovery_at = checked_at - timedelta(seconds=6)
    healthy_at = checked_at - timedelta(seconds=4)
    replay_at = checked_at - timedelta(seconds=2)
    stale_samples = _incident_replay_samples(
        [
            first_outage_at,
            first_recovery_at,
            second_outage_at,
            second_recovery_at,
            replay_at,
        ],
        "temporal_unavailable",
    )
    _seed_delivered_incident(
        session_factory,
        "temporal_unavailable",
        previous_outage_at,
        previous_recovery_at,
    )
    stale_monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=stale_telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: stale_samples.pop(0),
    )
    current_monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=current_telegram,
        bot_token="token",
        chat_id="123",
        readiness_probe=lambda _service: SimpleNamespace(
            degraded_reasons=[], components={}, checked_at=healthy_at
        ),
    )
    record_snapshots = stale_monitor._record_snapshots

    def database_temporarily_unavailable(
        samples: list[tuple[list[str], list[str], datetime]],
    ) -> None:
        raise ConnectionError("database unavailable")

    stale_monitor._record_snapshots = database_temporarily_unavailable
    for _ in range(4):
        asyncio.run(stale_monitor.run_once())
    asyncio.run(current_monitor.run_once())
    stale_monitor._record_snapshots = record_snapshots
    asyncio.run(stale_monitor.run_once())

    with session_scope(session_factory) as session:
        repository = ReadinessAlertRepository(session)
        for observed_at, reasons in (
            (first_outage_at, ["temporal_unavailable"]),
            (first_recovery_at, []),
            (second_outage_at, ["temporal_unavailable"]),
            (second_recovery_at, []),
        ):
            repository.record_snapshot(reasons, observed_at=observed_at)
    _assert_replayed_incidents(session_factory, stale_telegram, current_telegram)


def test_earlier_buffered_incident_is_retained_after_later_incident(session_factory) -> None:
    now = utc_now()
    reason = "temporal_unavailable"
    first_down, first_up, later_down, later_up, current = (
        now - timedelta(seconds=seconds) for seconds in (50, 40, 30, 20, 10)
    )
    _seed_delivered_incident(session_factory, reason, later_down, later_up)
    telegram = FakeTelegram()
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="token",
        chat_id="123",
    )
    samples = [([reason], [], first_down), ([], [], first_up), ([], [], current)]
    monitor._record_snapshots(samples)
    monitor._record_snapshots(samples)
    deliveries = _delivery_rows(session_factory)
    assert sorted((item.transition_number, item.transition) for item in deliveries) == [
        (1, "degraded"),
        (2, "recovered"),
        (3, "degraded"),
        (4, "recovered"),
    ]
    with session_scope(session_factory) as session:
        state = session.get(ReadinessAlertState, reason)
        assert state is not None and not state.degraded


def test_buffered_samples_inside_recorded_incident_do_not_duplicate(session_factory) -> None:
    now = utc_now()
    reason = "temporal_unavailable"
    _seed_delivered_incident(
        session_factory, reason, now - timedelta(seconds=50), now - timedelta(seconds=20)
    )
    monitor = ReadinessAlertMonitor(
        task_service=object(),
        session_factory=session_factory,
        telegram_client=FakeTelegram(),
        bot_token="token",
        chat_id="123",
    )
    monitor._record_snapshots(
        [
            ([reason], [], now - timedelta(seconds=40)),
            ([], [], now - timedelta(seconds=30)),
            ([], [], now - timedelta(seconds=10)),
        ]
    )
    assert len(_delivery_rows(session_factory)) == 2


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
