"""Correlate incidents across evidence sources without exporting raw reasons."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from db.base import Base
from db.readiness_alert_models import ReadinessAlertAcknowledgment, ReadinessAlertDelivery
from db.webhook_alert_models import WebhookAlertDelivery
from orchestrator.scheduled_evidence import collect_evidence
from repositories import create_engine_from_url, create_session_factory, session_scope


def _seed_correlated_history(factory, observed, cutoff):
    first, second = "interaction_wait_stuck|private-task-a", "interaction_wait_stuck|private-task-b"
    with session_scope(factory) as session:
        for ref, reason in (("telegram-a", first), ("telegram-b", second)):
            session.add(
                ReadinessAlertDelivery(
                    id=ref,
                    reason=reason,
                    transition_number=1,
                    transition="degraded",
                    observed_at=observed,
                    status="pending",
                    attempts=0,
                    next_attempt_at=cutoff,
                )
            )
        session.add(
            WebhookAlertDelivery(
                id="webhook-a",
                destination_id="private-destination",
                reason=first,
                transition_number=1,
                transition="degraded",
                observed_at=observed,
                status="pending",
                attempts=0,
                next_attempt_at=cutoff,
            )
        )
        session.add(
            WebhookAlertDelivery(
                id="recovery-a",
                destination_id="private-destination",
                reason=first,
                transition_number=2,
                transition="recovered",
                observed_at=observed,
                status="pending",
                attempts=0,
                next_attempt_at=cutoff,
            )
        )
        session.add(
            ReadinessAlertAcknowledgment(
                id="ack-a",
                reason=first,
                transition_number=1,
                acknowledged_at=observed,
                acknowledged_by="private-operator",
            )
        )


def test_evidence_correlates_distinct_same_category_incidents(tmp_path, monkeypatch):
    cutoff = datetime(2026, 10, 31, tzinfo=UTC)
    observed = cutoff - timedelta(hours=1)
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'evidence.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    _seed_correlated_history(factory, observed, cutoff)
    monkeypatch.setattr("orchestrator.scheduled_evidence.utc_now", lambda: cutoff)
    service = SimpleNamespace(session_factory=factory)
    snapshot = collect_evidence(service, cutoff, 24)
    assert snapshot == collect_evidence(service, cutoff, 24)
    records = {
        row["reference"]: row
        for source in snapshot["sources"].values()
        for row in source["records"]
    }
    key = records["telegram-a"]["reason_reference"]
    assert key == records["webhook-a"]["reason_reference"]
    assert key == records["recovery-a"]["reason_reference"]
    assert key == records["ack-a"]["reason_reference"]
    assert key != records["telegram-b"]["reason_reference"]
    assert records["telegram-a"]["transition_number"] == records["ack-a"]["transition_number"]
    assert "private-" not in json.dumps(snapshot)
