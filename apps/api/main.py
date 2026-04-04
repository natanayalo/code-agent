"""Bootstrap FastAPI application for local development."""

from __future__ import annotations

from fastapi import FastAPI

from apps.api.routes.health import router as health_router
from apps.api.routes.tasks import router as tasks_router
from orchestrator.execution import TaskExecutionService


def create_app(*, task_service: TaskExecutionService | None = None) -> FastAPI:
    """Create a FastAPI app with optional task-execution dependencies."""
    app = FastAPI(
        title="code-agent",
        version="0.1.0",
        description="Bootstrap API for the code-agent service.",
    )
    app.state.task_service = task_service
    app.include_router(health_router)
    app.include_router(tasks_router)
    return app


app = create_app()
