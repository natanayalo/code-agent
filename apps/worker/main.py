"""Dedicated Temporal worker runtime for production-like deployment."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from sqlalchemy.orm import sessionmaker

from apps.api.progress import create_outbound_http_clients
from apps.api.task_service_factory import build_task_service_from_env
from apps.observability import configure_tracing_from_env
from apps.runtime import (
    RUN_WORKER_ENV_VAR,
    initialize_persisted_cutover,
    should_run_worker,
    validate_cutover_configuration,
)
from orchestrator.temporal.worker import start_temporal_worker

logger = logging.getLogger(__name__)


async def run_worker_forever() -> None:
    """Build the task service and run the Temporal worker forever."""
    if not should_run_worker():
        raise RuntimeError(
            f"Worker runtime is disabled for this process. Set {RUN_WORKER_ENV_VAR}=1 to enable it."
        )
    validate_cutover_configuration()

    configure_tracing_from_env(service_name="code-agent-worker")

    outbound_http_clients = create_outbound_http_clients()
    try:
        service = build_task_service_from_env(outbound_http_clients=outbound_http_clients)
        if service is None:
            raise RuntimeError(
                "Worker runtime requires CODE_AGENT_ENABLE_TASK_SERVICE=1 "
                "with a valid database configuration."
            )
        if isinstance(getattr(service, "session_factory", None), sessionmaker):
            initialize_persisted_cutover(service.session_factory)

        temporal_address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
        await start_temporal_worker(
            temporal_address=temporal_address,
            task_queue="task-execution-queue",
            task_service=service,
        )
    finally:
        await asyncio.gather(
            outbound_http_clients.telegram.aclose(),
            outbound_http_clients.webhook.aclose(),
            return_exceptions=True,
        )


async def run_worker_until_stopped() -> None:
    """Run the worker until completion or a graceful SIGTERM shutdown."""
    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    worker_task = asyncio.create_task(run_worker_forever())
    shutdown_task = asyncio.create_task(shutdown_requested.wait())
    signal_registered = False
    try:
        loop.add_signal_handler(signal.SIGTERM, shutdown_requested.set)
        signal_registered = True
        completed, _ = await asyncio.wait(
            {worker_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if shutdown_task in completed and worker_task not in completed:
            logger.info("SIGTERM received; shutting down Temporal worker")
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            return
        await worker_task
    finally:
        worker_task.cancel()
        shutdown_task.cancel()
        await asyncio.gather(worker_task, shutdown_task, return_exceptions=True)
        if signal_registered:
            loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    """CLI entrypoint for the Temporal worker runtime."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(run_worker_until_stopped())


if __name__ == "__main__":  # pragma: no cover
    main()
