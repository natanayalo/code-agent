"""Bootstrap FastAPI application for local development."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.progress import create_outbound_http_clients
from apps.api.routes.health import router as health_router
from apps.api.routes.tasks import router as tasks_router
from apps.api.routes.telegram import router as telegram_router
from apps.api.routes.webhook import router as webhook_router
from apps.api.task_service_factory import build_task_service_from_env
from orchestrator.execution import TaskExecutionService


def create_app(*, task_service: TaskExecutionService | None = None) -> FastAPI:
    """Create a FastAPI app with optional task-execution dependencies."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        outbound_http_clients = create_outbound_http_clients()
        try:
            app.state.outbound_http_clients = outbound_http_clients
            if task_service is None:
                app.state.task_service = build_task_service_from_env(
                    outbound_http_clients=outbound_http_clients
                )
            else:
                app.state.task_service = task_service
            yield
        finally:
            try:
                await outbound_http_clients.telegram.aclose()
            finally:
                await outbound_http_clients.webhook.aclose()

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
