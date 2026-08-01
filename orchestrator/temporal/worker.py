from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from db.enums import WorkerNodeStatus
from orchestrator.operational_health import WORKER_HEARTBEAT_INTERVAL_SECONDS
from orchestrator.temporal.activities import TaskExecutionActivities
from orchestrator.temporal.command_dispatcher import TemporalCommandDispatcher
from orchestrator.temporal.queues import CODEX_EXECUTION_TASK_QUEUE
from orchestrator.temporal.workflows import TaskExecutionWorkflow

logger = logging.getLogger(__name__)

TEMPORAL_WORKER_ID_ENV_VAR = "CODE_AGENT_TEMPORAL_WORKER_ID"


def _temporal_worker_identity() -> tuple[str, str]:
    hostname = socket.gethostname()
    worker_id = os.environ.get(TEMPORAL_WORKER_ID_ENV_VAR, "").strip() or hostname
    return worker_id, f"{hostname}:{os.getpid()}"


def _require_active_worker(status: WorkerNodeStatus | None) -> None:
    if status != WorkerNodeStatus.ACTIVE:
        rendered = status.value if status is not None else "missing"
        raise RuntimeError(f"Temporal worker registry status is {rendered}; refusing work.")


async def _register_temporal_worker(task_service: Any, *, worker_id: str) -> None:
    _, process_identity = _temporal_worker_identity()
    status = await task_service._run_blocking(
        task_service.register_worker_node,
        worker_id=worker_id,
        capacity=2,
        process_identity=process_identity,
    )
    _require_active_worker(status)
    logger.info(
        "Registered Temporal worker heartbeat",
        extra={"worker_id": worker_id, "process_identity": process_identity},
    )


async def _heartbeat_temporal_worker(task_service: Any, *, worker_id: str) -> None:
    while True:
        await asyncio.sleep(WORKER_HEARTBEAT_INTERVAL_SECONDS)
        status = await task_service._run_blocking(
            task_service.heartbeat_worker_node,
            worker_id=worker_id,
        )
        _require_active_worker(status)


def _build_temporal_workers(
    *, client: Client, task_queue: str, task_service: Any
) -> tuple[Worker, Worker]:
    activities = TaskExecutionActivities(service=task_service)
    workflow_worker = Worker(
        client,
        task_queue=task_queue,
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
            activities.record_workflow_failure,
            activities.verify_result,
            activities.deliver_result,
            activities.persist_memory,
        ],
    )
    execution_worker = Worker(
        client,
        task_queue=CODEX_EXECUTION_TASK_QUEUE,
        activities=[activities.run_worker, activities.run_decomposed_node],
        # M25.2's local backstop. The durable selector remains responsible for
        # choosing safe waves; this prevents a single worker process from
        # accepting an unbounded number of execution activities.
        max_concurrent_activities=2,
    )
    return workflow_worker, execution_worker


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
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            client = await Client.connect(temporal_address)
            break
        except Exception as exc:
            last_error = exc
            if attempt == 2:
                raise RuntimeError(
                    f"Temporal worker could not connect to {temporal_address} after 3 attempts."
                ) from exc
            logger.warning(
                "Temporal worker connection failed; retrying",
                extra={"attempt": attempt + 1},
            )
            await asyncio.sleep(2**attempt)
    else:  # pragma: no cover - loop either breaks or raises
        raise RuntimeError("Temporal worker connection retries exhausted.") from last_error

    worker_id, _ = _temporal_worker_identity()
    await _register_temporal_worker(task_service, worker_id=worker_id)

    workflow_worker, codex_execution_worker = _build_temporal_workers(
        client=client,
        task_queue=task_queue,
        task_service=task_service,
    )

    logger.info("Temporal workers successfully started. Running worker loops...")
    dispatcher = TemporalCommandDispatcher(
        client=client, session_factory=task_service.session_factory
    )
    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(_heartbeat_temporal_worker(task_service, worker_id=worker_id))
        task_group.create_task(dispatcher.run_forever())
        task_group.create_task(workflow_worker.run())
        task_group.create_task(codex_execution_worker.run())
