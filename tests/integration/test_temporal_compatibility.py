"""Real-server compatibility checks for the pinned Temporal baseline.

Run only against the disposable stack in docker-compose.temporal-compatibility.yml.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleState,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.exceptions import CancelledError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from orchestrator.temporal.workflows import TaskExecutionWorkflow
from tests.temporal_history_fixtures import capture_history_fixture


def _server_address() -> str:
    address = os.environ.get("TEMPORAL_COMPATIBILITY_ADDRESS")
    if not address:
        pytest.skip("real-server compatibility gate is opt-in")
    if address != "127.0.0.1:17233":
        pytest.fail("Refusing a non-disposable Temporal target; use 127.0.0.1:17233")
    return address


def test_server_address_skips_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_COMPATIBILITY_ADDRESS", raising=False)
    with pytest.raises(pytest.skip.Exception, match="opt-in"):
        _server_address()


def test_server_address_rejects_non_disposable_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_COMPATIBILITY_ADDRESS", "temporal:7233")
    with pytest.raises(pytest.fail.Exception, match="Refusing a non-disposable Temporal target"):
        _server_address()


@activity.defn(name="classify_and_plan")
async def _classify_and_plan(_task_id: str) -> dict[str, object]:
    return {"requires_approval": True, "execution_task_queue": None}


@activity.defn(name="persist_approval_decision")
async def _persist_approval_decision(_task_id: str, _approved: bool) -> None:
    return None


@activity.defn(name="decompose_task")
async def _decompose_task(_task_id: str) -> dict[str, object]:
    return {"execution_shape": "monolithic", "execution_task_queue": None}


@activity.defn(name="load_memory")
async def _load_memory(_task_id: str) -> None:
    return None


@activity.defn(name="provision_workspace")
async def _provision_workspace(_task_id: str) -> None:
    return None


@activity.defn(name="run_worker")
async def _run_worker(_task_id: str) -> dict[str, bool]:
    return {"requires_permission_escalation": False}


@activity.defn(name="verify_result")
async def _verify_result(_task_id: str) -> dict[str, str]:
    return {"continuation": "complete", "summary": "compatibility check passed"}


@activity.defn(name="persist_memory")
async def _persist_memory(_task_id: str) -> None:
    return None


@activity.defn(name="deliver_result")
async def _deliver_result(_task_id: str) -> dict[str, str]:
    return {"status": "completed"}


@workflow.defn
class _ReadOnlyScheduleWorkflow:
    @workflow.run
    async def run(self, execution_id: str) -> str:
        return f"read-only schedule execution {execution_id}"


ACTIVITIES = [
    _classify_and_plan,
    _persist_approval_decision,
    _decompose_task,
    _load_memory,
    _provision_workspace,
    _run_worker,
    _verify_result,
    _persist_memory,
    _deliver_result,
]


async def _connect() -> Client:
    return await Client.connect(
        _server_address(),
        namespace="default",
    )


async def _wait_for_decision(handle: WorkflowHandle[Any, Any]) -> dict[str, object]:
    for _ in range(100):
        decision = await handle.query(TaskExecutionWorkflow.get_pending_decision)
        if decision:
            return decision
        await asyncio.sleep(0.1)
    pytest.fail("production workflow did not reach its approval wait")


@pytest.mark.anyio
async def test_production_workflow_queries_update_signal_restart_and_terminal_result() -> None:
    client = await _connect()
    task_queue = f"compat-{uuid.uuid4().hex}"
    workflow_id = f"compat-task-{uuid.uuid4().hex}"
    first_worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[TaskExecutionWorkflow],
        activities=ACTIVITIES,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    first_run = asyncio.create_task(first_worker.run())
    handle = await client.start_workflow(
        TaskExecutionWorkflow.run,
        workflow_id,
        id=workflow_id,
        task_queue=task_queue,
    )
    try:
        pending = await _wait_for_decision(handle)
        assert pending["kind"] == "approval"
        status = await handle.query(TaskExecutionWorkflow.get_status)
        assert status["lifecycle_stage"] == "awaiting_approval"

        # The execution remains durable while the first worker is stopped.
        await first_worker.shutdown()
        await first_run
        second_worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[TaskExecutionWorkflow],
            activities=ACTIVITIES,
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        second_run = asyncio.create_task(second_worker.run())
        try:
            for _ in range(100):
                try:
                    await handle.query(TaskExecutionWorkflow.get_status)
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            update = await handle.execute_update(
                TaskExecutionWorkflow.apply_operator_decision,
                {"decision_kind": "approval", "value": True, "command_key": "compat-approve"},
            )
            assert update["applied"] is True
            assert (await handle.result()) == {
                "status": "completed",
                "summary": "Task completed successfully via Temporal.",
            }
            capture_history_fixture("approval-update", await handle.fetch_history())
        finally:
            await second_worker.shutdown()
            await second_run
    finally:
        if not first_run.done():
            await first_worker.shutdown()
            await first_run


@pytest.mark.anyio
async def test_production_workflow_signal_and_cancellation() -> None:
    client = await _connect()
    task_queue = f"compat-{uuid.uuid4().hex}"
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[TaskExecutionWorkflow],
        activities=ACTIVITIES,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    worker_run = asyncio.create_task(worker.run())
    signal_id = f"compat-signal-{uuid.uuid4().hex}"
    cancel_id = f"compat-cancel-{uuid.uuid4().hex}"
    try:
        signaled = await client.start_workflow(
            TaskExecutionWorkflow.run, signal_id, id=signal_id, task_queue=task_queue
        )
        await _wait_for_decision(signaled)
        await signaled.signal(TaskExecutionWorkflow.handle_approval, True)
        assert (await signaled.result())["status"] == "completed"

        cancelled = await client.start_workflow(
            TaskExecutionWorkflow.run, cancel_id, id=cancel_id, task_queue=task_queue
        )
        await _wait_for_decision(cancelled)
        await cancelled.cancel()
        with pytest.raises(WorkflowFailureError) as failure:
            await cancelled.result()
        assert isinstance(failure.value.cause, CancelledError)
        capture_history_fixture("cancellation", await cancelled.fetch_history())
    finally:
        await worker.shutdown()
        await worker_run


@pytest.mark.anyio
async def test_paused_schedule_can_be_triggered_and_deleted() -> None:
    client = await _connect()
    task_queue = f"compat-schedule-{uuid.uuid4().hex}"
    workflow_id = f"compat-scheduled-run-{uuid.uuid4().hex}"
    schedule_id = f"compat-schedule-{uuid.uuid4().hex}"
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[_ReadOnlyScheduleWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    worker_run = asyncio.create_task(worker.run())
    schedule_handle = None
    try:
        schedule_handle = await client.create_schedule(
            schedule_id,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    _ReadOnlyScheduleWorkflow.run,
                    workflow_id,
                    id=workflow_id,
                    task_queue=task_queue,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(days=1))]),
                state=ScheduleState(paused=True),
            ),
        )
        await schedule_handle.trigger()
        execution_id = None
        for _ in range(100):
            description = await schedule_handle.describe()
            if description.info.recent_actions:
                execution_id = description.info.recent_actions[-1].action.workflow_id
                break
            await asyncio.sleep(0.1)
        if execution_id is None:
            pytest.fail(
                f"triggered schedule did not start its workflow: {await schedule_handle.describe()}"
            )
        result = await client.get_workflow_handle(execution_id).result()
        assert result == f"read-only schedule execution {workflow_id}"
    finally:
        try:
            if schedule_handle is not None:
                await schedule_handle.delete()
        finally:
            await worker.shutdown()
            await worker_run
