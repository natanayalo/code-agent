"""Replay compatibility coverage for the M25.2 fan-out patch boundary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from orchestrator.temporal.policy import activity_options
from orchestrator.temporal.workflows import (
    CANCELLATION_TERMINAL_PATCH_ID,
    NODE_WAVE_PATCH_ID,
    REJECTED_SESSION_STATE_PATCH_ID,
    TaskExecutionWorkflow,
)
from tests.temporal_history_fixtures import capture_history_fixture


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


@activity.defn(name="persist_rejected_session_state")
async def _persist_rejected_session_state(_task_id: str) -> dict:
    return {}


@activity.defn(name="run_worker")
async def _run_worker(_task_id: str) -> dict:
    return {"requires_permission_escalation": False}


@workflow.defn(name="TaskExecutionWorkflow")
class _M30PriorApprovalWorkflow:
    """Approval command sequence recorded before decision persistence was added."""

    def __init__(self) -> None:
        self.approved: bool | None = None

    @workflow.run
    async def run(self, task_id: str) -> dict:
        workflow.patched(CANCELLATION_TERMINAL_PATCH_ID)
        result = await workflow.execute_activity(
            "classify_and_plan", task_id, **activity_options("classify_and_plan")
        )
        if result.get("requires_approval"):
            await workflow.wait_condition(lambda: self.approved is not None)
            if not self.approved:
                if workflow.patched(REJECTED_SESSION_STATE_PATCH_ID):
                    await workflow.execute_activity(
                        "persist_rejected_session_state",
                        task_id,
                        **activity_options("persist_rejected_session_state"),
                    )
                return {"status": "rejected", "summary": "Manual approval rejected."}

        decomposition = await workflow.execute_activity(
            "decompose_task", task_id, **activity_options("decompose_task")
        )
        await workflow.execute_activity("load_memory", task_id, **activity_options("load_memory"))
        await workflow.execute_activity(
            "provision_workspace", task_id, **activity_options("provision_workspace")
        )
        await workflow.execute_activity(
            "run_worker",
            task_id,
            **activity_options("run_worker", task_queue=decomposition.get("execution_task_queue")),
        )
        if workflow.patched("m25-4-temporal-completion-loop"):
            await workflow.execute_activity(
                "verify_result", task_id, **activity_options("verify_result")
            )
        await workflow.execute_activity(
            "persist_memory", task_id, **activity_options("persist_memory")
        )
        if workflow.patched("task-delivery-acceptance-v1"):
            await workflow.execute_activity(
                "deliver_result", task_id, **activity_options("deliver_result")
            )
        return {"status": "completed"}

    @workflow.signal
    def handle_approval(self, approved: bool) -> None:
        self.approved = approved


@activity.defn(name="classify_and_plan")
async def _classify_for_approval(_task_id: str) -> dict:
    return {"requires_approval": True}


@activity.defn(name="decompose_task")
async def _decompose_monolithic(_task_id: str) -> dict:
    return {"execution_shape": "monolithic", "execution_task_queue": None}


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
            capture_history_fixture("decomposed-patch-false", history)

    replay = await Replayer(
        workflows=[TaskExecutionWorkflow], workflow_runner=UnsandboxedWorkflowRunner()
    ).replay_workflow(history)

    assert replay.replay_failure is None


@pytest.mark.anyio
async def test_m25_4_repair_history_replays_with_completion_loop_patch() -> None:
    """A new repair-loop history must remain deterministic under the current workflow."""
    verification_calls = 0
    worker_calls = 0

    @activity.defn(name="decompose_task")
    async def decompose_monolithic(_task_id: str) -> dict:
        return {"execution_shape": "monolithic", "execution_task_queue": None}

    @activity.defn(name="run_worker")
    async def run_worker(_task_id: str) -> dict:
        nonlocal worker_calls
        worker_calls += 1
        return {"requires_permission_escalation": False}

    @activity.defn(name="verify_result")
    async def verify_with_repair(_task_id: str) -> dict:
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 1:
            return {
                "continuation": "repair",
                "repair_source": "verifier",
                "repair_pass": 1,
                "summary": "verifier requested repair pass 1",
            }
        return {
            "continuation": "complete",
            "repair_source": "verifier",
            "repair_pass": 1,
            "summary": "repair accepted",
        }

    activities = [
        _classify_and_plan,
        decompose_monolithic,
        _load_memory,
        _provision_workspace,
        run_worker,
        verify_with_repair,
        _persist_memory,
        _deliver_result,
    ]
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="repair-replay-test",
            workflows=[TaskExecutionWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                TaskExecutionWorkflow.run,
                "task-id",
                id="m25-4-repair-history",
                task_queue="repair-replay-test",
            )
            assert (await handle.result())["status"] == "completed"
            history = await handle.fetch_history()
            capture_history_fixture("repair", history)

    assert worker_calls == 2
    assert verification_calls == 2
    replay = await Replayer(
        workflows=[TaskExecutionWorkflow], workflow_runner=UnsandboxedWorkflowRunner()
    ).replay_workflow(history)

    assert replay.replay_failure is None


@pytest.mark.anyio
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "rejected"])
async def test_pre_persistence_approval_history_replays(approved: bool) -> None:
    """Legacy approval histories retain their command sequence after the new activity."""
    activities = [
        _classify_for_approval,
        _decompose_monolithic,
        _load_memory,
        _provision_workspace,
        _run_worker,
        _verify_result,
        _persist_memory,
        _deliver_result,
        _persist_rejected_session_state,
    ]
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="approval-replay-test",
            workflows=[_M30PriorApprovalWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                _M30PriorApprovalWorkflow.run,
                "task-id",
                id=f"m30-approval-history-{approved}",
                task_queue="approval-replay-test",
            )
            await handle.signal(_M30PriorApprovalWorkflow.handle_approval, approved)
            await handle.result()
            history = await handle.fetch_history()
            decision = "approved" if approved else "rejected"
            capture_history_fixture(f"approval-{decision}", history)

    replay = await Replayer(
        workflows=[TaskExecutionWorkflow], workflow_runner=UnsandboxedWorkflowRunner()
    ).replay_workflow(history)

    assert replay.replay_failure is None


@pytest.mark.anyio
async def test_sanitized_frozen_temporal_histories_replay() -> None:
    """Frozen approval, cancellation, repair, and decomposed histories replay."""
    from pathlib import Path

    from temporalio.client import WorkflowHistory

    fixture_dir = Path(__file__).parent / "fixtures" / "temporal_histories"
    fixture_names = (
        "approval-approved",
        "approval-rejected",
        "approval-update",
        "cancellation",
        "repair",
        "decomposed-patch-false",
    )
    replayer = Replayer(
        workflows=[TaskExecutionWorkflow], workflow_runner=UnsandboxedWorkflowRunner()
    )
    for fixture_name in fixture_names:
        path = fixture_dir / f"{fixture_name}.json"
        assert path.is_file(), f"missing frozen Temporal history fixture: {path}"
        history = WorkflowHistory.from_json(f"frozen-{fixture_name}", path.read_text("utf-8"))
        replay = await replayer.replay_workflow(history)
        assert replay.replay_failure is None, f"{fixture_name}: {replay.replay_failure}"
