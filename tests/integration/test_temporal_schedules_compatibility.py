"""Real-server tests for weekly schedules."""

from __future__ import annotations

import asyncio
import uuid
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import HTTPException
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from apps.api.auth import ApiAuthConfig
from apps.api.config import SystemConfig
from apps.api.main import create_app
from apps.api.routes.schedules import (
    create_schedule,
    get_schedule,
    get_schedule_client,
    manage_schedule,
)
from db.base import Base
from orchestrator.execution import TaskExecutionService
from orchestrator.execution_types import DeliveryKey
from orchestrator.schedule_spec import ScheduleSpec as ReviewSpec
from orchestrator.scheduled_submission import build_scheduled_submission
from orchestrator.temporal.queues import DEFAULT_TEMPORAL_TASK_QUEUE
from orchestrator.temporal.scheduled_review import (
    ScheduledReviewActivities,
    ScheduledReviewWorkflow,
)
from repositories import create_engine_from_url, create_session_factory
from tests.integration.test_temporal_compatibility import _connect
from tests.unit.task_execution_service_support import _StaticWorker


async def _wait_for_scheduled_result(client, handle):
    for _ in range(150):
        description = await handle.describe()
        if description.info.recent_actions:
            occurrence = description.info.recent_actions[-1].action
            return await asyncio.wait_for(
                client.get_workflow_handle(occurrence.workflow_id).result(), timeout=30
            )
        await asyncio.sleep(0.1)
    pytest.fail("scheduled occurrence did not start")


class FailAfterCommit:
    def __init__(self, service):
        self.service = service
        self.attempts = 0

    def __getattr__(self, name):
        return getattr(self.service, name)

    def create_task_outcome(self, *args, **kwargs):
        result = self.service.create_task_outcome(*args, **kwargs)
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("simulated lost activity response")
        return result


@pytest.mark.anyio
@pytest.mark.parametrize("purpose", ["dependency_review", "health_summary"])
async def test_weekly_review_recovers_worker_and_deduplicates_submission_retry(
    tmp_path, purpose
) -> None:
    """The real server persists paused schedules and retries one logical occurrence."""
    client = await _connect()
    schedule_id = f"m31-{uuid.uuid4().hex}"
    config = SystemConfig(
        default_image="test",
        workspace_root=str(tmp_path),
        allowed_repos={"repo": "https://example.com/repo"},
    )
    spec = ReviewSpec(
        version=1,
        purpose=purpose,
        repo_key="repo",
        weekdays=("monday",),
        time="09:00",
        timezone="America/New_York",
    )
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    service = TaskExecutionService(
        session_factory=create_session_factory(engine), worker=_StaticWorker()
    )

    wrapper = FailAfterCommit(service)
    activities = ScheduledReviewActivities(wrapper, config)
    handle = client.get_schedule_handle(f"dependency-review:{schedule_id}")
    try:
        created = await create_schedule(schedule_id, spec, config, client)
        assert created["paused"] is True
        assert created["next_executions"]
        for execution in created["next_executions"]:
            local = execution.astimezone(ZoneInfo(spec.timezone))
            assert local.weekday() == 0 and (local.hour, local.minute) == (9, 0)
        assert (await create_schedule(schedule_id, spec, config, client))["paused"] is True
        removed = SystemConfig(default_image="test", workspace_root=str(tmp_path))
        assert (await create_schedule(schedule_id, spec, removed, client))["paused"] is True
        with pytest.raises(HTTPException) as conflict:
            await create_schedule(
                schedule_id, spec.model_copy(update={"time": "10:00"}), removed, client
            )
        assert conflict.value.status_code == 409
        await manage_schedule(schedule_id, "trigger", client)
        # Trigger while no worker is present, then start it to prove recovery.
        async with Worker(
            client,
            task_queue=DEFAULT_TEMPORAL_TASK_QUEUE,
            workflows=[ScheduledReviewWorkflow],
            activities=[activities.submit],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            result = await _wait_for_scheduled_result(client, handle)
            assert result["outcome"] == "duplicate"
            assert wrapper.attempts >= 2
            assert len(service.list_tasks()) == 1
            view = await get_schedule(schedule_id, client)
            assert view["recent_actions"][-1]["outcome"] == "duplicate"
            await manage_schedule(schedule_id, "trigger", client)
            for _ in range(100):
                view = await get_schedule(schedule_id, client)
                if (
                    len(view["recent_actions"]) == 2
                    and view["recent_actions"][-1].get("outcome") == "skipped_busy"
                ):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("busy occurrence did not report a skip")
            assert len(service.list_tasks()) == 1
        assert not (await manage_schedule(schedule_id, "resume", client))["paused"]
        assert (await manage_schedule(schedule_id, "pause", client))["paused"]
    finally:
        await handle.delete()


@pytest.mark.anyio
@pytest.mark.parametrize("purpose", ["dependency_review", "health_summary"])
async def test_schedule_http_management_requires_auth_and_is_idempotent(tmp_path, purpose) -> None:
    temporal = await _connect()
    schedule_id = f"m31-api-{uuid.uuid4().hex}"
    engine = create_engine_from_url(f"sqlite+pysqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    service = TaskExecutionService(
        session_factory=create_session_factory(engine), worker=_StaticWorker()
    )
    app = create_app(task_service=service)
    app.state.task_service = service
    app.state.api_auth_config = ApiAuthConfig(shared_secret="test-secret")  # gitleaks:allow
    app.state.system_config = SystemConfig(
        default_image="test",
        workspace_root=str(tmp_path),
        allowed_repos={"repo": "https://example.com/repo"},
    )
    app.dependency_overrides[get_schedule_client] = lambda: temporal
    spec = {
        "version": 1,
        "purpose": purpose,
        "repo_key": "repo",
        "weekdays": ["monday"],
        "time": "09:00",
        "timezone": "UTC",
    }
    headers = {"X-Webhook-Token": "test-secret"}  # gitleaks:allow
    handle = temporal.get_schedule_handle(f"dependency-review:{schedule_id}")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            assert (await http.get("/schedules")).status_code == 401
            response = await http.put(f"/schedules/{schedule_id}", json=spec, headers=headers)
            assert response.status_code == 200 and response.json()["paused"]
            assert (
                await http.put(f"/schedules/{schedule_id}", json=spec, headers=headers)
            ).status_code == 200
            assert (
                await http.put(
                    f"/schedules/{schedule_id}", json=spec | {"time": "10:00"}, headers=headers
                )
            ).status_code == 409
            assert (
                await http.put(
                    f"/schedules/{schedule_id}",
                    json=spec | {"task_text": "modify"},
                    headers=headers,
                )
            ).status_code == 422
            assert any(
                item["id"] == schedule_id
                for item in (await http.get("/schedules", headers=headers)).json()
            )
            assert (await http.get(f"/schedules/{schedule_id}/runs", headers=headers)).json() == []
            submission = build_scheduled_submission(
                schedule_id=schedule_id,
                spec=ReviewSpec.model_validate(spec),
                repo_url="https://example.com/repo",
                occurrence_id="occurrence",
                health_snapshot={"readiness": {"status": "unknown"}}
                if purpose == "health_summary"
                else None,
            )
            created = service.create_task_outcome(
                submission,
                delivery_key=DeliveryKey(channel="schedule", delivery_id="occurrence"),
                skip_if_session_busy=True,
            )
            runs = (await http.get(f"/schedules/{schedule_id}/runs", headers=headers)).json()
            assert len(runs) == 1 and runs[0]["task"]["task_id"] == created.task_snapshot.task_id
            assert (await http.get(runs[0]["timeline_url"], headers=headers)).status_code == 200
            assert not (
                await http.post(f"/schedules/{schedule_id}/resume", headers=headers)
            ).json()["paused"]
            assert (await http.post(f"/schedules/{schedule_id}/pause", headers=headers)).json()[
                "paused"
            ]
    finally:
        await handle.delete()
