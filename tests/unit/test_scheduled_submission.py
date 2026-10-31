"""The scheduled task has no caller-controlled execution options."""

import pytest

from orchestrator.scheduled_submission import build_scheduled_submission
from tests.unit.test_schedule_spec import valid_spec


def test_scheduled_submission_is_fixed_read_only_and_uses_stable_session():
    submission = build_scheduled_submission(
        schedule_id="weekly", spec=valid_spec(branch="main"), repo_url="https://example.com/repo"
    )
    assert submission.repo_url == "https://example.com/repo"
    assert submission.branch == "main"
    assert submission.constraints == {
        "read_only": True,
        "delivery_mode": "summary",
        "schedule_id": "weekly",
        "occurrence_id": None,
    }
    assert submission.session.channel == "schedule"
    assert submission.session.external_thread_id == "weekly"
    assert not submission.secret_refs
    assert "outdated" in submission.task_text
    assert "evidence" in submission.task_text
    assert "limitations" in submission.task_text


def test_health_summary_submission_contains_safe_snapshot_and_fixed_constraints():
    submission = build_scheduled_submission(
        schedule_id="health",
        spec=valid_spec(purpose="health_summary"),
        repo_url="repo",
        health_snapshot={"readiness": {"status": "not_ready"}},
    )
    assert '"not_ready"' in submission.task_text
    assert submission.constraints["read_only"] is True
    assert submission.constraints["delivery_mode"] == "summary"
    assert "unsupported diagnoses" in submission.constraints["acceptance_criteria"][0]


def test_health_summary_requires_snapshot():
    with pytest.raises(ValueError, match="require an observation snapshot"):
        build_scheduled_submission(
            schedule_id="health", spec=valid_spec(purpose="health_summary"), repo_url="repo"
        )


def test_evidence_collection_requires_snapshot():
    with pytest.raises(ValueError, match="requires a snapshot"):
        build_scheduled_submission(
            schedule_id="evidence",
            spec=valid_spec(
                purpose="evidence_collection",
                evidence={"source": "operational_incidents", "lookback_hours": 24},
            ),
            repo_url="repo",
        )
