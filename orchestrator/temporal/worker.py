from __future__ import annotations

import asyncio
import logging
from typing import Any

from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from orchestrator.temporal.activities import TaskExecutionActivities
from orchestrator.temporal.queues import CODEX_EXECUTION_TASK_QUEUE
from orchestrator.temporal.workflows import TaskExecutionWorkflow

logger = logging.getLogger(__name__)


async def start_temporal_worker(
    temporal_address: str,
    task_queue: str,
    task_service: Any,
) -> None:
    """Connect to Temporal and start the worker loop."""
    logger.info(
        "Starting Temporal worker",
        extra={"address": temporal_address, "queue": task_queue},
    )
    client = await Client.connect(temporal_address)

    # Initialize our activities class with the service
    activities = TaskExecutionActivities(service=task_service)

    # Run the worker until cancelled
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[TaskExecutionWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
        activities=[
            activities.classify_and_plan,
            activities.decompose_task,
            activities.select_next_node,
            activities.merge_node_wave,
            activities.fail_node_permission_escalation,
            activities.load_memory,
            activities.provision_workspace,
            activities.run_worker,
            activities.run_decomposed_node,
            activities.request_permission_escalation,
            activities.resolve_permission_escalation,
            activities.record_workflow_failure,
            activities.verify_result,
            activities.deliver_result,
            activities.persist_memory,
        ],
    )

    codex_execution_worker = Worker(
        client,
        task_queue=CODEX_EXECUTION_TASK_QUEUE,
        activities=[activities.run_worker, activities.run_decomposed_node],
        # M25.2's local backstop. The durable selector remains responsible for
        # choosing safe waves; this prevents a single worker process from
        # accepting an unbounded number of execution activities.
        max_concurrent_activities=2,
    )

    logger.info("Temporal workers successfully started. Running worker loops...")
    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(worker.run())
        task_group.create_task(codex_execution_worker.run())
