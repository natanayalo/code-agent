"""Task submission and status routes for the vertical-slice API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.dependencies import get_task_service, require_api_auth
from orchestrator.execution import TaskExecutionService, TaskSnapshot, TaskSubmission

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_api_auth)])


@router.post("", response_model=TaskSnapshot, status_code=status.HTTP_202_ACCEPTED)
def submit_task(
    payload: TaskSubmission,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Create a task, enqueue it for worker pickup, and return the pollable snapshot."""
    task_snapshot, _ = task_service.create_task(payload)
    return task_snapshot


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
