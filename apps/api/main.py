"""Bootstrap FastAPI application for local development."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.auth import (
    API_SHARED_SECRET_ENV_VAR,
    ApiAuthConfig,
    build_api_auth_config_from_env,
)
from apps.api.progress import create_outbound_http_clients
from apps.api.routes.health import router as health_router
from apps.api.routes.tasks import router as tasks_router
from apps.api.routes.telegram import router as telegram_router
from apps.api.routes.webhook import router as webhook_router
from apps.api.task_service_factory import build_task_service_from_env
from orchestrator.execution import TaskExecutionService, shutdown_callback_dns_executor

logger = logging.getLogger(__name__)


def create_app(
    *,
    task_service: TaskExecutionService | None = None,
    auth_config: ApiAuthConfig | None = None,
) -> FastAPI:
    """Create a FastAPI app with optional task-execution dependencies."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if task_service is None:
            outbound_http_clients = create_outbound_http_clients()
            try:
                app.state.outbound_http_clients = outbound_http_clients
                app.state.api_auth_config = (
                    auth_config if auth_config is not None else build_api_auth_config_from_env()
                )
                app.state.task_service = build_task_service_from_env(
                    outbound_http_clients=outbound_http_clients
                )
                if (
                    app.state.task_service is not None
                    and app.state.api_auth_config.shared_secret is None
                ):
                    raise RuntimeError(
                        "Task service bootstrap requires "
                        f"{API_SHARED_SECRET_ENV_VAR} to protect /tasks and /webhook."
                    )
                yield
            finally:
                results = await asyncio.gather(
                    outbound_http_clients.telegram.aclose(),
                    outbound_http_clients.webhook.aclose(),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning(
                            "Failed to close outbound HTTP client during app shutdown",
                            exc_info=result,
                        )
                shutdown_callback_dns_executor()
        else:
            try:
                app.state.api_auth_config = auth_config or ApiAuthConfig()
                app.state.task_service = task_service
                yield
            finally:
                shutdown_callback_dns_executor()

    app = FastAPI(
        title="code-agent",
        version="0.1.0",
        description="Bootstrap API for the code-agent service.",
        lifespan=lifespan,
    )
    app.include_router(health_router)
    app.include_router(tasks_router)
    app.include_router(webhook_router)
    app.include_router(telegram_router)
    return app


app = create_app()
