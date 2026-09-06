"""Task acceptance agrees across activity results, persisted state and API projections."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.pool import StaticPool
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from db.base import Base
from db.enums import TaskStatus, TimelineEventType
from orchestrator.execution import TaskExecutionService, TaskSubmission
from orchestrator.state import OrchestratorState
from orchestrator.temporal.activities import TaskExecutionActivities
from orchestrator.temporal.workflows import ACCEPTANCE_PATCH_ID, TaskExecutionWorkflow
from repositories import (
    TaskTimelineRepository,
    TemporalTaskStateRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from workers import Worker


class UnusedWorker(Worker):
    async def run(self, request, *, system_prompt=None):
        pytest.fail("A failed environment must not dispatch a fallback worker")


@pytest.fixture
def harness():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = TaskExecutionService(session_factory=factory, worker=UnusedWorker())
    snapshot, _ = service.create_task(TaskSubmission(task_text="Produce a draft PR"))
    activities = TaskExecutionActivities(service)
    state = activities._get_current_state(snapshot.task_id)
    payload = state.model_dump()
    payload.update(
        current_step="verify_result",
        task_spec={"goal": "Produce a draft PR", "delivery_mode": "draft_pr"},
        result={"status": "success", "summary": "Changes made", "files_changed": ["fix.py"]},
        verification={"status": "passed"},
    )
    yield service, activities, OrchestratorState.model_validate(payload)
    engine.dispose()


def persist(harness, state):
    service, _, _ = harness
    with session_scope(service.session_factory) as session:
        TemporalTaskStateRepository(session).upsert(
            task_id=state.task.task_id, state=state.model_dump(mode="json")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable_verifier", [False, True])
async def test_missing_delivery_cannot_complete_and_retry_is_idempotent(
    harness, unavailable_verifier
):
    service, activities, state = harness
    if unavailable_verifier:
        state = OrchestratorState.model_validate(
            {
                **state.model_dump(),
                "verification": {
                    "status": "warning",
                    "items": [
                        {
                            "label": "independent_verifier",
                            "status": "warning",
                            "reason_code": "provider_auth",
                        }
                    ],
                },
            }
        )
    persist(harness, state)
    first = await activities.deliver_result(state.task.task_id)
    assert first["status"] == "failed"
    snapshot = service.get_task(state.task.task_id)
    assert snapshot.status == TaskStatus.FAILED
    assert snapshot.latest_run.files_changed == ["fix.py"]
    expected = "infra_verifier_unavailable" if unavailable_verifier else "incomplete_delivery"
    with session_scope(service.session_factory) as session:
        events = TaskTimelineRepository(session).list_by_task(state.task.task_id)
        assert expected in {(event.payload or {}).get("failure_kind") for event in events}
    activities.deliver_result_node = AsyncMock(
        side_effect=AssertionError("must not repeat delivery")
    )
    assert await activities.deliver_result(state.task.task_id) == first


@pytest.mark.asyncio
async def test_broker_confirmed_delivery_completes_once(harness):
    service, activities, state = harness
    persist(harness, state)
    result = state.result.model_copy(
        update={
            "delivery_metadata": {
                "branch_name": "task/test",
                "pr_url": "https://github.com/example/repo/pull/1",
            }
        }
    )
    activities.deliver_result_node = AsyncMock(
        spec=activities.deliver_result_node,
        return_value={
            "result": result,
            "timeline_events": [
                {
                    "event_type": "delivery_completed",
                    "attempt_number": state.attempt_count,
                    "payload": {"branch": "task/test"},
                }
            ],
        },
    )
    assert (await activities.deliver_result(state.task.task_id))["status"] == "completed"
    assert service.get_task(state.task.task_id).status == TaskStatus.COMPLETED
    assert (await activities.deliver_result(state.task.task_id))["status"] == "completed"
    activities.deliver_result_node.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_draft_pr_does_not_complete_when_current_broker_push_fails(
    harness, monkeypatch, tmp_path
):
    service, activities, state = harness
    state.dispatch.workspace_id = "existing-workspace"
    workspace_root = tmp_path / "workspaces"
    (workspace_root / "existing-workspace").mkdir(parents=True)
    (workspace_root / ".code-agent-git" / "existing-workspace").mkdir(parents=True)
    monkeypatch.setenv("GH_TOKEN", "broker-token")
    monkeypatch.setattr("sandbox.workspace.default_workspace_root", lambda: workspace_root)
    persist(harness, state)

    existing_pr = {
        "delivery_mode": "draft_pr",
        "branch_name": f"task/{state.task.task_id}",
        "pr_url": "https://github.com/example/repo/pull/1",
        "head_sha": "stale-pr-head",
    }
    with (
        patch(
            "orchestrator.nodes.delivery._capture_delivery_metadata",
            return_value=existing_pr,
        ),
        patch(
            "orchestrator.nodes.delivery._run_broker_git_commands",
            return_value=("Delivery failed to push branch: rejected", "delivery failed (git push)"),
        ),
    ):
        outcome = await activities.deliver_result(state.task.task_id)

    assert outcome == {"status": "failed"}
    snapshot = service.get_task(state.task.task_id)
    assert snapshot.status == TaskStatus.FAILED
    assert snapshot.latest_run.delivery_metadata is None
    with session_scope(service.session_factory) as session:
        events = TaskTimelineRepository(session).list_by_task(state.task.task_id)
    assert TimelineEventType.DELIVERY_FAILED in {event.event_type for event in events}
    assert TimelineEventType.DELIVERY_COMPLETED not in {event.event_type for event in events}


@pytest.mark.asyncio
async def test_failed_setup_keeps_identity_and_blocks_worker(harness):
    from orchestrator.nodes.provisioning import _init_fail

    _, activities, state = harness
    state.dispatch.workspace_id = "provisioned-workspace"
    payload = state.model_dump()
    payload.update(_init_fail(state, "Missing required lockfile"))
    state = OrchestratorState.model_validate(payload)
    assert state.dispatch.workspace_id == "provisioned-workspace"
    persist(harness, state)
    activities.await_result_node = AsyncMock(side_effect=AssertionError("must not execute"))
    await activities.run_worker(state.task.task_id)
    activities.await_result_node.assert_not_awaited()


@pytest.mark.asyncio
async def test_fatal_setup_projects_failure_before_workflow_can_dispatch(harness):
    from orchestrator.nodes.provisioning import _init_fail

    service, activities, state = harness
    state.result = None
    state.verification = None
    persist(harness, state)

    async def provision(_state):
        return {"dispatch": {"workspace_id": "existing-workspace", "worker_type": "codex"}}

    async def initialize(raw_state):
        return _init_fail(OrchestratorState.model_validate(raw_state), "Missing required lockfile")

    activities.provision_workspace_node = provision
    activities.init_environment_node = initialize
    with pytest.raises(ApplicationError) as error:
        await activities.provision_workspace(state.task.task_id)
    assert error.value.non_retryable
    snapshot = service.get_task(state.task.task_id)
    assert snapshot.status == TaskStatus.FAILED
    assert snapshot.latest_run.verifier_outcome["failure_kind"] == "sandbox_infra"


@pytest.mark.asyncio
@pytest.mark.parametrize("patched, expected", [(True, "failed"), (False, "completed")])
async def test_workflow_terminal_result_is_versioned(monkeypatch, patched, expected):
    monkeypatch.setattr(workflow, "patched", lambda name: patched and name == ACCEPTANCE_PATCH_ID)
    monkeypatch.setattr(workflow, "execute_activity", AsyncMock(return_value={"status": "failed"}))
    outcome = await TaskExecutionWorkflow()._persist_and_deliver("task", {})
    assert outcome["status"] == expected
