"""Task submission and status routes for the vertical-slice API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from apps.api.dependencies import get_task_service, require_api_auth
from orchestrator.execution import (
    TaskApprovalDecision,
    TaskExecutionService,
    TaskSnapshot,
    TaskSubmission,
)

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


@router.post("/{task_id}/approval", response_model=TaskSnapshot)
def decide_task_approval(
    task_id: str,
    payload: TaskApprovalDecision,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Apply an idempotent manual approval decision for a paused task."""
    result = task_service.apply_task_approval_decision(task_id=task_id, approved=payload.approved)
    if result.status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.detail or f"Task '{task_id}' was not found.",
        )
    if result.status == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.detail or "Task decision conflicts with an existing approval decision.",
        )
    if result.status == "not_waiting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.detail or "Task is not awaiting approval.",
        )
    if result.task_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Task decision was applied but the task snapshot could not be reloaded.",
        )
    return result.task_snapshot
