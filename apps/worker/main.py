"""Dedicated queue worker runtime for production-like deployment."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Final
from uuid import uuid4

from apps.api.progress import create_outbound_http_clients
from apps.api.task_service_factory import build_task_service_from_env
from apps.observability import bootstrap_langsmith_otel
from apps.runtime import (
    RUN_WORKER_ENV_VAR,
    should_run_worker,
)
from apps.runtime import (
    coerce_positive_int_env as _coerce_positive_int,
)
from orchestrator.execution import TaskQueueWorker

logger = logging.getLogger(__name__)

WORKER_ID_ENV_VAR: Final[str] = "CODE_AGENT_QUEUE_WORKER_ID"
POLL_INTERVAL_ENV_VAR: Final[str] = "CODE_AGENT_QUEUE_POLL_INTERVAL_SECONDS"
LEASE_SECONDS_ENV_VAR: Final[str] = "CODE_AGENT_QUEUE_LEASE_SECONDS"


def _coerce_positive_float(value: str | None, *, default: float) -> float:
    if value is None:
        return default
    stripped = value.strip()
    if not stripped:
        return default
    try:
        parsed = float(stripped)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


async def run_worker_forever() -> None:
    """Build task service from env and start polling forever."""
    if not should_run_worker():
        raise RuntimeError(
            f"Worker runtime is disabled for this process. Set {RUN_WORKER_ENV_VAR}=1 to enable it."
        )

    outbound_http_clients = create_outbound_http_clients()
    try:
        service = build_task_service_from_env(outbound_http_clients=outbound_http_clients)
        if service is None:
            raise RuntimeError(
                "Worker runtime requires CODE_AGENT_ENABLE_TASK_SERVICE=1 "
                "with a valid database configuration."
            )

        worker_id = os.environ.get(WORKER_ID_ENV_VAR, "").strip() or f"worker-{uuid4().hex[:8]}"
        poll_interval = _coerce_positive_float(
            os.environ.get(POLL_INTERVAL_ENV_VAR),
            default=2.0,
        )
        lease_seconds = _coerce_positive_int(
            os.environ.get(LEASE_SECONDS_ENV_VAR),
            default=60,
        )

        queue_worker = TaskQueueWorker(
            service=service,
            worker_id=worker_id,
            poll_interval_seconds=poll_interval,
            lease_seconds=lease_seconds,
        )
        await queue_worker.run_forever()
    finally:
        await asyncio.gather(
            outbound_http_clients.telegram.aclose(),
            outbound_http_clients.webhook.aclose(),
            return_exceptions=True,
        )


def main() -> None:
    """CLI entrypoint for the queue worker runtime."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    bootstrap_langsmith_otel(runtime_name="worker", logger=logger)
    asyncio.run(run_worker_forever())


if __name__ == "__main__":  # pragma: no cover
    main()
