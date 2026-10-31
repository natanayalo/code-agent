"""Scheduled evidence reads real history without exposing private fields."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from db.base import Base
from db.readiness_alert_models import (
    ReadinessAlertAcknowledgment,
    ReadinessAlertDelivery,
    ReadinessAlertState,
)
from db.webhook_alert_models import WebhookAlertDelivery
from orchestrator.scheduled_evidence import collect_evidence
from orchestrator.scheduled_submission import build_scheduled_submission
from repositories import create_engine_from_url, create_session_factory, session_scope
from tests.unit.test_schedule_spec import valid_spec


def test_evidence_configuration_contract():
    spec = valid_spec(
        purpose="evidence_collection",
        evidence={"source": "operational_incidents", "lookback_hours": 24},
    )
    assert spec.model_dump(mode="json")["evidence"]["lookback_hours"] == 24
    assert "evidence" not in valid_spec().model_dump(mode="json")
    for value in (0, 169, True, "24"):
        with pytest.raises(ValidationError):
            valid_spec(
                purpose="evidence_collection",
                evidence={"source": "operational_incidents", "lookback_hours": value},
            )
    with pytest.raises(ValidationError):
        valid_spec(purpose="evidence_collection")
    with pytest.raises(ValidationError):
        valid_spec(evidence={"source": "operational_incidents", "lookback_hours": 24})


def _seed_history(factory, start, cutoff):
    with session_scope(factory) as session:
        session.add(
            ReadinessAlertState(
                reason="postgres_unavailable",
                degraded=True,
                transition_number=1,
                last_transition_at=start,
                last_checked_at=start,
            )
        )
        for i in range(102):
            session.add(
                ReadinessAlertDelivery(
                    id=f"{i:036}",
                    reason="interaction_wait_stuck|private-task",
                    transition_number=i,
                    transition="degraded",
                    observed_at=start + timedelta(seconds=i),
                    status="pending",
                    attempts=0,
                    next_attempt_at=cutoff,
                )
            )
        session.add(
            ReadinessAlertDelivery(
                id="boundary",
                reason="secret",
                transition_number=1,
                transition="degraded",
                observed_at=cutoff,
                status="pending",
                attempts=0,
                next_attempt_at=cutoff,
            )
        )
        session.add(
            WebhookAlertDelivery(
                id="webhook",
                destination_id="private-destination",
                reason="postgres_unavailable",
                transition_number=1,
                transition="recovered",
                observed_at=start,
                status="delivered",
                attempts=1,
                next_attempt_at=cutoff,
            )
        )
        session.add(
            ReadinessAlertAcknowledgment(
                id="ack",
                reason="provider|private-node|private-provider",
                transition_number=1,
                acknowledged_at=start,
                acknowledged_by="private-operator",
            )
        )


def test_collection_interval_sanitization_and_bounds(tmp_path):
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'evidence.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    cutoff = datetime(2026, 10, 31, tzinfo=UTC)
    start = cutoff - timedelta(hours=24)
    _seed_history(factory, start, cutoff)
    payload = collect_evidence(SimpleNamespace(session_factory=factory), cutoff, 24)
    telegram = payload["sources"]["telegram_deliveries"]
    assert telegram["status"] == "truncated" and telegram["omitted_count"] == 2
    assert len(telegram["records"]) == 100
    assert telegram["records"][0]["reference"] == f"{101:036}"
    assert payload["sources"]["webhook_deliveries"]["status"] == "observed"
    assert (
        payload["sources"]["acknowledgments"]["records"][0]["reason_category"]
        == "provider_capability"
    )
    assert payload["latest_observation_at"] == start.isoformat()
    encoded = json.dumps(payload, sort_keys=True)
    assert len(encoded.encode()) <= 65536
    assert "private-" not in encoded and "boundary" not in encoded
    assert payload["truncated"]
    submission = build_scheduled_submission(
        schedule_id="evidence",
        spec=valid_spec(
            purpose="evidence_collection",
            evidence={"source": "operational_incidents", "lookback_hours": 24},
        ),
        repo_url="repo",
        evidence_snapshot=payload,
    )
    assert submission.constraints["operational_evidence"] == payload
    assert submission.constraints["read_only"]


def test_unavailable_sources_are_explicit():
    payload = collect_evidence(object(), datetime(2026, 10, 31, tzinfo=UTC), 24)
    assert all(source["status"] == "unavailable" for source in payload["sources"].values())
    assert payload["latest_observation_at"] is None


def test_duplicate_and_cancellation_preserve_accepted_snapshot(tmp_path):
    from db.enums import TaskStatus
    from orchestrator.execution import TaskExecutionService
    from orchestrator.execution_submission_service import ScheduledSessionBusyError
    from orchestrator.execution_types import DeliveryKey
    from tests.unit.task_execution_service_support import _StaticWorker

    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    service = TaskExecutionService(
        session_factory=create_session_factory(engine), worker=_StaticWorker()
    )
    spec = valid_spec(
        purpose="evidence_collection",
        evidence={"source": "operational_incidents", "lookback_hours": 24},
    )

    def submit(snapshot):
        return build_scheduled_submission(
            schedule_id="evidence", spec=spec, repo_url="repo", evidence_snapshot=snapshot
        )

    key = DeliveryKey(channel="schedule", delivery_id="occurrence")
    first = service.create_task_outcome(
        submit({"version": 1}), delivery_key=key, skip_if_session_busy=True
    )
    duplicate = service.create_task_outcome(
        submit({"version": 2}), delivery_key=key, skip_if_session_busy=True
    )
    assert duplicate.duplicate and duplicate.task_snapshot.task_id == first.task_snapshot.task_id
    with pytest.raises(ScheduledSessionBusyError):
        service.create_task_outcome(
            submit({"version": 3}),
            delivery_key=DeliveryKey(channel="schedule", delivery_id="next"),
            skip_if_session_busy=True,
        )
    cancelled = service.cancel_task(task_id=first.task_snapshot.task_id)
    assert cancelled is not None and cancelled.status == TaskStatus.CANCELLED.value
    task = service.get_task(first.task_snapshot.task_id)
    assert task is not None and task.constraints["operational_evidence"] == {"version": 1}


@pytest.mark.parametrize("status", ["success", "failure"])
def test_evidence_artifact_is_independent_of_worker_output(tmp_path, status):
    from orchestrator.execution_outcome_service import _persist_execution_outcome
    from orchestrator.state import OrchestratorState
    from repositories import TaskRepository, WorkerRunRepository
    from tests.integration.test_execution_outcome_service import MockExecutionService, _setup_task

    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'outcome.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        task_id = _setup_task(session)
        task = TaskRepository(session).get(task_id)
        task.constraints = {"operational_evidence": {"version": 1}}
    state = OrchestratorState.model_validate(
        {
            "task": {"task_text": "Summarize", "task_id": task_id},
            "route": {"chosen_worker": "codex", "route_reason": "test"},
            "dispatch": {},
            "approval": {"required": False, "status": "not_required"},
            "result": {
                "status": status,
                "summary": "worker output",
                "artifacts": [
                    {
                        "name": "operational-evidence.json",
                        "uri": "inline://worker",
                        "artifact_type": "result_summary",
                        "artifact_metadata": {"fake": True},
                    }
                ],
            },
        }
    )
    now = datetime.now(UTC)
    _persist_execution_outcome(
        MockExecutionService(factory),
        task_id=task_id,
        state=state,
        started_at=now,
        finished_at=now,
        persist_friction_proposals=False,
    )
    with session_scope(factory) as session:
        run = WorkerRunRepository(session).list_by_task(task_id)[0]
        entry = next(
            item for item in run.artifact_index if item["name"] == "operational-evidence.json"
        )
        assert sum(item["name"] == "operational-evidence.json" for item in run.artifact_index) == 1
        assert entry["artifact_metadata"]["operational_evidence"] == {"version": 1}
        assert any(
            item.name == "operational-evidence.json"
            and item.artifact_metadata["operational_evidence"] == {"version": 1}
            for item in run.artifacts
        )
