"""Scheduled activity validates eligibility and preserves occurrence identity."""

from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError

from apps.api.config import SystemConfig
from orchestrator.execution_submission_service import ScheduledSessionBusyError
from orchestrator.temporal.scheduled_review import ScheduledReviewActivities
from tests.unit.test_schedule_spec import valid_spec


@pytest.mark.anyio
async def test_activity_revalidates_allowlist():
    activity = ScheduledReviewActivities(
        object(), SystemConfig(default_image="test", workspace_root="/tmp")
    )
    with pytest.raises(ApplicationError, match="no longer allowlisted") as error:
        await activity.submit("schedule", "occurrence", valid_spec().model_dump(mode="json"))
    assert error.value.non_retryable


@pytest.mark.anyio
async def test_activity_uses_stable_delivery_key_and_read_only_submission():
    calls = []

    class Service:
        def create_task_outcome(self, submission, **kwargs):
            calls.append((submission, kwargs))
            return SimpleNamespace(
                duplicate=len(calls) > 1, task_snapshot=SimpleNamespace(task_id="task")
            )

    activity = ScheduledReviewActivities(
        Service(),
        SystemConfig(
            default_image="test", workspace_root="/tmp", allowed_repos={"project": "repo"}
        ),
    )
    spec = valid_spec().model_dump(mode="json")
    assert (await activity.submit("schedule", "occurrence", spec))["outcome"] == "submitted"
    assert (await activity.submit("schedule", "occurrence", spec))["outcome"] == "duplicate"
    assert calls[0][1]["delivery_key"] == calls[1][1]["delivery_key"]
    assert calls[0][0].constraints["read_only"] is True
    assert calls[0][0].constraints["occurrence_id"] == "occurrence"
    assert calls[0][1]["skip_if_session_busy"] is True


@pytest.mark.anyio
async def test_activity_surfaces_busy_skip():
    class Service:
        def create_task_outcome(self, submission, **kwargs):
            raise ScheduledSessionBusyError("busy")

    activity = ScheduledReviewActivities(
        Service(),
        SystemConfig(
            default_image="test", workspace_root="/tmp", allowed_repos={"project": "repo"}
        ),
    )
    result = await activity.submit("schedule", "occurrence", valid_spec().model_dump(mode="json"))
    assert result == {"outcome": "skipped_busy", "task_id": None}
