"""The scheduled task has no caller-controlled execution options."""

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
