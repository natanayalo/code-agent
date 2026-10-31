"""Scheduled observations expose only bounded health data."""

from datetime import timedelta
from types import SimpleNamespace

from db.base import Base, utc_now
from db.readiness_alert_models import ReadinessAlertState
from orchestrator.operational_health_types import ExecutionHealthMetrics, ReadinessSnapshot
from orchestrator.scheduled_health import _incidents, collect_health_snapshot
from repositories import (
    ReadinessAlertRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)


def test_incident_snapshot_reports_healthy_baseline_and_watermark(tmp_path):
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'health.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    now = utc_now()
    service = SimpleNamespace(session_factory=factory)
    assert _incidents(service) == {
        "status": "unknown",
        "active_count": None,
        "latest_observation_at": None,
    }
    with session_scope(factory) as session:
        ReadinessAlertRepository(session).record_snapshot([], observed_at=now)
    baseline = _incidents(service)
    assert baseline["status"] == "observed"
    assert baseline["active_count"] == 0
    assert baseline["latest_observation_at"] is not None
    later = now + timedelta(minutes=1)
    with session_scope(factory) as session:
        ReadinessAlertRepository(session).record_interaction_snapshot([], observed_at=later)
    watermark = _incidents(service)
    assert watermark["active_count"] == 0
    assert watermark["latest_observation_at"].startswith(later.strftime("%Y-%m-%dT%H:%M:%S"))


def test_snapshot_filters_private_data_and_aggregates_incidents(tmp_path):
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'health.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    now = utc_now()
    with session_scope(factory) as session:
        session.add(
            ReadinessAlertState(
                reason="interaction_wait_stuck|private-task",
                degraded=True,
                transition_number=1,
                last_transition_at=now,
                last_checked_at=now,
            )
        )
    readiness = ReadinessSnapshot.model_validate(
        {
            "status": "not_ready",
            "checked_at": now,
            "components": {"worker": {"status": "not_ready", "reasons": ["secret"]}},
            "degraded_reasons": ["worker_unavailable", "secret"],
        }
    )
    metrics = ExecutionHealthMetrics.model_validate(
        {
            "outbox": {
                "pending_count": 1,
                "retrying_count": 0,
                "dead_letter_count": 0,
                "affected_task_ids": ["private-task"],
            },
            "workers": {"fresh_count": 0, "stale_count": 1, "fresh_dispatcher_count": 0},
            "interactions": {
                "pending_count": 1,
                "stuck_count": 1,
                "affected_task_ids": ["private-task"],
            },
            "reconciliation": {
                "status": "unknown",
                "checked_at": now,
                "affected_task_ids": ["private-task"],
            },
            "degraded_reasons": ["worker_unavailable", "secret"],
        }
    )
    service = SimpleNamespace(
        session_factory=factory,
        get_readiness=lambda: readiness,
        get_execution_health=lambda: metrics,
    )
    snapshot = collect_health_snapshot(service)
    assert snapshot["incidents"]["active_count"] == 1
    assert snapshot["readiness"]["components"]["postgres"]["status"] == "unknown"
    assert snapshot["execution_health"]["reconciliation"]["status"] == "unknown"
    assert "private-task" not in str(snapshot)
    assert "secret" not in str(snapshot)


def test_snapshot_records_independent_failures_without_error_details():
    snapshot = collect_health_snapshot(object())
    assert all(
        snapshot[key] == {"status": "unavailable"}
        for key in ("readiness", "execution_health", "incidents")
    )
