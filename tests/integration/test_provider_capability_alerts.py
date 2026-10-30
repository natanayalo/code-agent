"""Worker observations flow through durable alerts and authenticated inspection."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from apps.api.readiness_alerts import ReadinessAlertMonitor
from db.base import utc_now
from db.readiness_alert_models import ReadinessAlertState
from orchestrator.execution import TaskExecutionService
from orchestrator.provider_observations import fresh_observations, save_observations
from repositories import ReadinessAlertRepository, WorkerNodeRepository, session_scope
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET, _default_worker
from tests.integration.test_readiness_alerts import FakeTelegram, _snapshot
from tests.integration.test_repositories_memory import postgres_session_factory  # noqa: F401


def sample(outcome, when, check="credentials"):
    return {
        "provider": "codex",
        "check": check,
        "outcome": outcome,
        "verification_scope": "local_presence",
        "observed_at": when.isoformat(),
    }


def test_provider_alert_lifecycle_and_fencing(session_factory):
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    now = utc_now()
    with session_scope(session_factory) as session:
        node = WorkerNodeRepository(session).register_worker(
            worker_id="worker-a",
            worker_type="codex",
            process_identity="process-a",
            now=now,
            capabilities={"runtime": "temporal"},
        )
        node_id = node.id
    telegram = FakeTelegram([500, 200, 200])
    monitor = ReadinessAlertMonitor(
        task_service=service,
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="secret",
        chat_id="1",
        readiness_probe=lambda _: _snapshot(),
        interaction_probe=lambda *_: [],
        outbox_probe=lambda *_: [],
        reconciliation_probe=lambda *_: [],
    )
    assert save_observations(service, "worker-a", "process-a", [sample("unready", now)])
    asyncio.run(monitor.run_once())
    asyncio.run(monitor.run_once())
    reason = f"provider|{node_id}|codex|credentials"
    with session_scope(session_factory) as session:
        repo = ReadinessAlertRepository(session)
        states = {s.reason: s for s in repo.list_states()}
        assert states[reason].degraded and states[reason].transition_number == 1
        deliveries = repo.list_deliveries()
        assert len(deliveries) == 1 and deliveries[0].attempts == 1
        deliveries[0].next_attempt_at = now - timedelta(seconds=1)
        repo.record_snapshot([], observed_at=now + timedelta(seconds=1))
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 2
    # Unknown, stale and older evidence cannot recover the incident.
    for item in [
        sample("unknown", now),
        sample("ready", now - timedelta(seconds=91)),
        sample("ready", now),
    ]:
        assert save_observations(service, "worker-a", "process-a", [item])
        asyncio.run(monitor.run_once())
    with session_scope(session_factory) as session:
        assert session.get(ReadinessAlertState, reason).degraded
    healthy_at = utc_now()
    assert save_observations(service, "worker-a", "process-a", [sample("ready", healthy_at)])
    asyncio.run(monitor.run_once())
    app = create_app(
        task_service=service, auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET)
    )
    with TestClient(app) as client:
        assert client.get("/alerts/readiness").status_code == 401
        response = client.get(
            "/alerts/readiness", headers={"X-Webhook-Token": DEFAULT_SHARED_SECRET}
        )
        state = next(s for s in response.json() if s["reason"] == "provider_capability")
        assert state["worker_node_id"] == node_id and state["provider"] == "codex"
        assert state["capability"] == "credentials" and state["degraded"] is False
        assert state["next_action"] and "process-a" not in response.text
    with session_scope(session_factory) as session:
        WorkerNodeRepository(session).register_worker(
            worker_id="worker-a",
            worker_type="codex",
            process_identity="process-b",
            now=utc_now(),
            capabilities={"runtime": "temporal"},
        )
    assert not save_observations(service, "worker-a", "process-a", [sample("unready", utc_now())])
    with session_scope(session_factory) as session:
        node = WorkerNodeRepository(session).get_by_worker_id("worker-a")
        assert node.capabilities == {"runtime": "temporal"}


def test_independent_workers_recurrence_and_replay(session_factory):
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    now = utc_now()
    with session_scope(session_factory) as session:
        repo = WorkerNodeRepository(session)
        first = repo.register_worker(
            worker_id="one", worker_type="codex", process_identity="p1", now=now
        )
        second = repo.register_worker(
            worker_id="two", worker_type="codex", process_identity="p2", now=now
        )
        first_id, second_id = first.id, second.id
    assert save_observations(
        service, "one", "p1", [sample("unready", now), sample("unready", now, "docker_daemon")]
    )
    assert save_observations(service, "two", "p2", [sample("unready", now)])
    monitor = ReadinessAlertMonitor(
        task_service=service,
        session_factory=session_factory,
        telegram_client=FakeTelegram(),
        bot_token="secret",
        chat_id="1",
    )
    monitor._record_provider_observations()
    with session_scope(session_factory) as session:
        repo = ReadinessAlertRepository(session)
        assert len(repo.list_states()) == 3
        repo.record_snapshot([], observed_at=now + timedelta(seconds=1))
        repo.record_earlier_incidents(
            [([], [], now + timedelta(seconds=2))], captured=repo.capture_observed_states()
        )
        assert all(state.degraded for state in repo.list_states())
    newer = utc_now()
    assert save_observations(service, "one", "p1", [sample("ready", newer)])
    monitor._record_provider_observations()
    newest = utc_now()
    assert save_observations(service, "one", "p1", [sample("unready", newest)])
    monitor._record_provider_observations()
    with session_scope(session_factory) as session:
        states = {state.reason: state for state in ReadinessAlertRepository(session).list_states()}
        assert states[f"provider|{first_id}|codex|credentials"].transition_number == 3
        assert states[f"provider|{first_id}|codex|docker_daemon"].degraded
        assert states[f"provider|{second_id}|codex|credentials"].transition_number == 1


def test_invalid_or_unavailable_samples_are_ignored(session_factory):
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    now = utc_now()
    with session_scope(session_factory) as session:
        WorkerNodeRepository(session).register_worker(
            worker_id="test", worker_type="codex", process_identity="p", now=now
        )
    bad_time = sample("ready", now)
    bad_time["observed_at"] = "not-a-date"
    bad_scope = sample("ready", now)
    bad_scope["verification_scope"] = "private"
    with session_scope(session_factory) as session:
        assert fresh_observations(session, now) == []
    assert save_observations(service, "test", "p", [None, bad_time, bad_scope])
    with session_scope(session_factory) as session:
        assert fresh_observations(session, utc_now()) == []


def test_activation_uses_only_current_observation(session_factory):
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    now = utc_now()
    with session_scope(session_factory) as session:
        WorkerNodeRepository(session).register_worker(
            worker_id="test", worker_type="codex", process_identity="p", now=now
        )
    assert save_observations(service, "test", "p", [sample("unready", now)])
    assert save_observations(service, "test", "p", [sample("ready", utc_now())])
    monitor = ReadinessAlertMonitor(
        task_service=service,
        session_factory=session_factory,
        telegram_client=FakeTelegram(),
        bot_token="secret",
        chat_id="1",
    )
    monitor._record_provider_observations()
    with session_scope(session_factory) as session:
        assert ReadinessAlertRepository(session).list_states() == []
        assert ReadinessAlertRepository(session).list_deliveries() == []


def test_concurrent_monitors_deduplicate_provider_transition(session_factory):
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    now = utc_now()
    with session_scope(session_factory) as session:
        WorkerNodeRepository(session).register_worker(
            worker_id="test", worker_type="codex", process_identity="p", now=now
        )
    assert save_observations(service, "test", "p", [sample("unready", now)])
    telegram = FakeTelegram()
    monitors = [
        ReadinessAlertMonitor(
            task_service=service,
            session_factory=session_factory,
            telegram_client=telegram,
            bot_token="secret",
            chat_id="1",
            readiness_probe=lambda _: _snapshot(),
            interaction_probe=lambda *_: [],
            outbox_probe=lambda *_: [],
            reconciliation_probe=lambda *_: [],
        )
        for _ in range(2)
    ]

    async def run():
        await asyncio.gather(*(monitor.run_once() for monitor in monitors))

    asyncio.run(run())
    with session_scope(session_factory) as session:
        assert len(ReadinessAlertRepository(session).list_deliveries()) == 1
    assert len(telegram.messages) == 1


def test_postgres_concurrent_provider_writes(postgres_session_factory):  # noqa: F811
    reason = f"provider|{uuid4()}|codex|credentials"
    now = utc_now()
    barrier = Barrier(2)

    def record(_):
        with session_scope(postgres_session_factory) as session:
            barrier.wait()
            ReadinessAlertRepository(session).record_provider_observations([(reason, True, now)])

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(record, range(2)))
    with session_scope(postgres_session_factory) as session:
        repo = ReadinessAlertRepository(session)
        assert len(repo.list_states()) == len(repo.list_deliveries()) == 1


def test_provider_monitor_failure_does_not_block_other_delivery(session_factory, monkeypatch):
    service = TaskExecutionService(session_factory=session_factory, worker=_default_worker())
    telegram = FakeTelegram()
    monitor = ReadinessAlertMonitor(
        task_service=service,
        session_factory=session_factory,
        telegram_client=telegram,
        bot_token="secret",
        chat_id="1",
        readiness_probe=lambda _: _snapshot("worker_unavailable"),
        interaction_probe=lambda *_: [],
        outbox_probe=lambda *_: [],
        reconciliation_probe=lambda *_: [],
    )

    def failed():
        raise RuntimeError("private diagnostic text")

    monkeypatch.setattr(monitor, "_record_provider_observations", failed)
    asyncio.run(monitor.run_once())
    assert len(telegram.messages) == 1
    assert "private diagnostic text" not in telegram.messages[0]["text"]
