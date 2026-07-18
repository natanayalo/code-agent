"""Replay compatibility coverage for the M25.2 fan-out patch boundary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from orchestrator.temporal.workflows import NODE_WAVE_PATCH_ID, TaskExecutionWorkflow


@activity.defn(name="classify_and_plan")
async def _classify_and_plan(_task_id: str) -> dict:
    return {}


@activity.defn(name="decompose_task")
async def _decompose_task(_task_id: str) -> dict:
    return {"execution_shape": "decomposed"}


@activity.defn(name="load_memory")
async def _load_memory(_task_id: str) -> dict:
    return {}


@activity.defn(name="provision_workspace")
async def _provision_workspace(_task_id: str) -> dict:
    return {}


@activity.defn(name="select_next_node")
async def _select_next_node(_task_id: str) -> dict:
    return {"action": "complete"}


@activity.defn(name="verify_result")
async def _verify_result(_task_id: str) -> dict:
    return {}


@activity.defn(name="persist_memory")
async def _persist_memory(_task_id: str) -> dict:
    return {}


@activity.defn(name="deliver_result")
async def _deliver_result(_task_id: str) -> dict:
    return {}


@workflow.defn(name="TaskExecutionWorkflow")
class _M25_1BWorkflow:
    """The pre-M25.2 command sequence, including its one-argument selector."""

    @workflow.run
    async def run(self, task_id: str) -> dict:
        lifecycle_activities = (
            "classify_and_plan",
            "decompose_task",
            "load_memory",
            "provision_workspace",
        )
        for activity_name in lifecycle_activities:
            await workflow.execute_activity(
                activity_name, task_id, start_to_close_timeout=timedelta(minutes=1)
            )
        if workflow.patched(NODE_WAVE_PATCH_ID):
            selection = await workflow.execute_activity(
                "select_next_node", task_id, start_to_close_timeout=timedelta(minutes=1)
            )
            assert selection["action"] == "complete"
        for activity_name in ("verify_result", "persist_memory", "deliver_result"):
            await workflow.execute_activity(
                activity_name, task_id, start_to_close_timeout=timedelta(minutes=1)
            )
        return {"status": "completed"}


@pytest.mark.anyio
async def test_m25_1b_history_replays_without_changing_select_next_node_input() -> None:
    """A recorded old selector command replays through patch-false V2 code."""
    activities = [
        _classify_and_plan,
        _decompose_task,
        _load_memory,
        _provision_workspace,
        _select_next_node,
        _verify_result,
        _persist_memory,
        _deliver_result,
    ]
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="replay-test",
            workflows=[_M25_1BWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                _M25_1BWorkflow.run,
                "task-id",
                id="m25-1b-history",
                task_queue="replay-test",
            )
            assert (await handle.result())["status"] == "completed"
            history = await handle.fetch_history()

    replay = await Replayer(
        workflows=[TaskExecutionWorkflow], workflow_runner=UnsandboxedWorkflowRunner()
    ).replay_workflow(history)

    assert replay.replay_failure is None
