"""FastAPI dependency helpers for the API entrypoints."""

from __future__ import annotations

from typing import cast

from fastapi import HTTPException, Request, status

from orchestrator.execution import TaskExecutionService


def get_task_service(request: Request) -> TaskExecutionService:
    """Return the configured task service or fail clearly when unavailable."""
    task_service = cast(
        TaskExecutionService | None, getattr(request.app.state, "task_service", None)
    )
    if task_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Task execution service is not configured for this app instance.",
        )
    return task_service
