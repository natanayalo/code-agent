"""Task submission and status routes for the vertical-slice API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.dependencies import get_task_service
from orchestrator.execution import TaskExecutionService, TaskSnapshot, TaskSubmission

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskSnapshot, status_code=status.HTTP_201_CREATED)
def submit_task(
    payload: TaskSubmission,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Create a task, execute it through the orchestrator, and persist the outcome."""
    return task_service.submit_task(payload)


@router.get("/{task_id}", response_model=TaskSnapshot)
def get_task(
    task_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Return the latest persisted state for a submitted task."""
    task_snapshot = task_service.get_task(task_id)
    if task_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' was not found.",
        )
    return task_snapshot
