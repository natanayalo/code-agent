"""Milestone readiness review endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from apps.api.dependencies import get_task_service, require_any_valid_auth
from orchestrator.execution import TaskExecutionService
from orchestrator.execution_types import (
    MilestoneDecision,
    MilestoneReadinessSnapshot,
    MilestoneSnapshot,
)

router = APIRouter(tags=["milestones"], dependencies=[Depends(require_any_valid_auth)])


@router.get("/milestones", response_model=list[MilestoneSnapshot])
def list_milestones(
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[MilestoneSnapshot]:
    """List tracked milestones and their active bounded policy."""
    return task_service.list_milestones()


@router.get("/milestone-readiness-assessments", response_model=list[MilestoneReadinessSnapshot])
def list_readiness_assessments(
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[MilestoneReadinessSnapshot]:
    return task_service.list_milestone_readiness_assessments()


@router.post(
    "/milestone-readiness-assessments/{assessment_id}/approve",
    response_model=MilestoneReadinessSnapshot,
)
def approve_readiness_assessment(
    assessment_id: UUID,
    payload: MilestoneDecision,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> MilestoneReadinessSnapshot:
    try:
        return task_service.decide_milestone_readiness(
            str(assessment_id), approved=True, mode=payload.mode, reason=payload.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/milestone-readiness-assessments/{assessment_id}/reject",
    response_model=MilestoneReadinessSnapshot,
)
def reject_readiness_assessment(
    assessment_id: UUID,
    payload: MilestoneDecision,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> MilestoneReadinessSnapshot:
    try:
        return task_service.decide_milestone_readiness(
            str(assessment_id), approved=False, mode=None, reason=payload.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/milestones/{milestone_id}/complete", response_model=MilestoneReadinessSnapshot)
def complete_milestone(
    milestone_id: UUID, task_service: TaskExecutionService = Depends(get_task_service)
) -> MilestoneReadinessSnapshot:
    """Complete a milestone and run its read-only successor readiness review."""
    try:
        return task_service.complete_milestone(str(milestone_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/milestones/{milestone_id}/reopen", response_model=MilestoneSnapshot)
def reopen_milestone(
    milestone_id: UUID, task_service: TaskExecutionService = Depends(get_task_service)
) -> MilestoneSnapshot:
    try:
        return task_service.reopen_milestone(str(milestone_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/milestones/{milestone_id}", response_model=MilestoneSnapshot)
def get_milestone(
    milestone_id: UUID, task_service: TaskExecutionService = Depends(get_task_service)
) -> MilestoneSnapshot:
    result = task_service.get_milestone(str(milestone_id))
    if result is None:
        raise HTTPException(status_code=404, detail="Milestone not found")
    return result
