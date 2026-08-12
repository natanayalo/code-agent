"""Shared helpers and fixtures for task endpoint integration tests."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner
from temporalio.worker import Worker as TemporalWorker

from orchestrator.temporal.activities import TaskExecutionActivities
from orchestrator.temporal.command_dispatcher import TemporalCommandDispatcher
from orchestrator.temporal.queues import CODEX_EXECUTION_TASK_QUEUE
from orchestrator.temporal.workflows import TaskExecutionWorkflow
from workers import Worker, WorkerRequest, WorkerResult

DEFAULT_SHARED_SECRET = "a" * 32  # gitleaks:allow


class StaticWorker(Worker):
    """Worker double that returns a predefined result and records requests."""

    def __init__(self, result: WorkerResult) -> None:
        self.result = result
        self.requests: list[WorkerRequest] = []

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.requests.append(request)
        return self.result


def _run_one_temporal_task(client: TestClient) -> None:
    """Dispatch one durable start command through the Temporal workflow."""
    service = client.app.state.task_service

    async def run() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            activities = TaskExecutionActivities(service=service)
            worker = TemporalWorker(
                environment.client,
                task_queue="task-execution-queue",
                workflows=[TaskExecutionWorkflow],
                workflow_runner=UnsandboxedWorkflowRunner(),
                activities=[
                    activities.classify_and_plan,
                    activities.decompose_task,
                    activities.select_next_node,
                    activities.select_next_node_v2,
                    activities.merge_node_wave,
                    activities.fail_node_permission_escalation,
                    activities.load_memory,
                    activities.provision_workspace,
                    activities.run_worker,
                    activities.run_decomposed_node,
                    activities.request_permission_escalation,
                    activities.resolve_permission_escalation,
                    activities.persist_rejected_session_state,
                    activities.record_workflow_failure,
                    activities.verify_result,
                    activities.deliver_result,
                    activities.persist_memory,
                ],
            )
            execution_worker = TemporalWorker(
                environment.client,
                task_queue=CODEX_EXECUTION_TASK_QUEUE,
                activities=[activities.run_worker, activities.run_decomposed_node],
            )
            async with worker, execution_worker:
                dispatcher = TemporalCommandDispatcher(
                    client=environment.client,
                    session_factory=service.session_factory,
                )
                await dispatcher.dispatch_pending()
                commands = service.list_tasks(limit=1)
                assert commands
                handle = environment.client.get_workflow_handle(f"task-{commands[0].task_id}")
                await handle.result()

    asyncio.run(run())


def _default_worker() -> StaticWorker:
    return StaticWorker(
        WorkerResult(
            status="success",
            summary="Created note.txt and retained the workspace for inspection.",
            budget_usage={"iterations_used": 2, "tool_calls_used": 1},
            commands_run=[
                {
                    "command": "printf 'done\\n' > note.txt",
                    "exit_code": 0,
                    "duration_seconds": 0.1,
                    "stdout_artifact_uri": "artifacts/stdout.log",
                    "stderr_artifact_uri": "artifacts/stderr.log",
                }
            ],
            files_changed=["note.txt"],
            artifacts=[
                {
                    "name": "workspace",
                    "uri": "/tmp/workspace-task-44-1234",
                    "artifact_type": "workspace",
                }
            ],
            next_action_hint="inspect_workspace_artifacts",
        )
    )
