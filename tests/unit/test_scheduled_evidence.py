"""Evidence collection bounds and original activity timestamp."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from temporalio.testing import ActivityEnvironment

from apps.api.config import SystemConfig
from orchestrator.scheduled_evidence import collect_evidence
from orchestrator.temporal.scheduled_review import ScheduledReviewActivities
from tests.unit.test_schedule_spec import valid_spec


def test_total_size_cap_reports_omissions(monkeypatch):
    class Context:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return False

    row = SimpleNamespace(
        id="x" * 1000,
        reason="postgres_unavailable",
        transition_number=1,
        observed_at=datetime.now(UTC),
        transition="degraded",
        status="pending",
        attempts=1,
        delivered_at=None,
        acknowledged_at=datetime.now(UTC),
    )
    monkeypatch.setattr("orchestrator.scheduled_evidence.session_scope", lambda factory: Context())
    monkeypatch.setattr("orchestrator.scheduled_evidence.latest_observation", lambda *args: None)
    monkeypatch.setattr(
        "orchestrator.scheduled_evidence.read_source", lambda *args: ([row] * 100, 100)
    )
    payload = collect_evidence(SimpleNamespace(session_factory=None), datetime.now(UTC), 24)
    assert len(json.dumps(payload, sort_keys=True).encode()) <= 65536
    assert payload["truncated"]
    assert sum(item["omitted_count"] for item in payload["sources"].values()) > 0


@pytest.mark.anyio
async def test_evidence_uses_original_scheduled_time_on_retry(monkeypatch):
    cutoff = datetime(2026, 10, 31, tzinfo=UTC)
    seen = []

    def collect(service, timestamp, hours):
        seen.append((timestamp, hours))
        return {"sources": {}, "truncated": False}

    monkeypatch.setattr("orchestrator.temporal.scheduled_review.collect_evidence", collect)

    class Service:
        def create_task_outcome(self, submission, **kwargs):
            return SimpleNamespace(
                duplicate=len(seen) > 1, task_snapshot=SimpleNamespace(task_id="task")
            )

    worker = ScheduledReviewActivities(
        Service(),
        SystemConfig(
            default_image="test", workspace_root="/tmp", allowed_repos={"project": "repo"}
        ),
    )
    env = ActivityEnvironment()
    env.info = replace(
        env.info,
        scheduled_time=cutoff,
        current_attempt_scheduled_time=datetime(2026, 11, 1, tzinfo=UTC),
    )
    spec = valid_spec(
        purpose="evidence_collection",
        evidence={"source": "operational_incidents", "lookback_hours": 24},
    ).model_dump(mode="json")
    assert (await env.run(worker.submit, "s", "o", spec))["outcome"] == "submitted"
    assert (await env.run(worker.submit, "s", "o", spec))["outcome"] == "duplicate"
    assert seen == [(cutoff, 24), (cutoff, 24)]
