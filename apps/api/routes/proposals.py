"""Proposal management and review routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apps.api.dependencies import get_task_service, require_any_valid_auth
from db.enums import ProposalStatus
from orchestrator.execution import (
    ProposalSnapshot,
    TaskExecutionService,
    TaskSnapshot,
)

router = APIRouter(
    prefix="/proposals",
    tags=["proposals"],
    dependencies=[Depends(require_any_valid_auth)],
)


@router.get("", response_model=list[ProposalSnapshot])
def list_proposals(
    status_filter: ProposalStatus | None = Query(None, alias="status"),
    session_id: str | None = None,
    task_id: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[ProposalSnapshot]:
    """List proposals with optional filtering and pagination."""
    return task_service.list_proposals(
        status=status_filter,
        session_id=session_id,
        task_id=task_id,
        limit=limit,
        offset=offset,
    )


@router.post("/{proposal_id}/accept", response_model=TaskSnapshot)
def accept_proposal(
    proposal_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Accept a proposal and promote it to a queued execution task."""
    result_status, task_snapshot, detail = task_service.accept_proposal(proposal_id)
    if result_status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail or f"Proposal '{proposal_id}' was not found.",
        )
    if result_status == "conflict":
        if task_snapshot is not None:
            # If it's already accepted and we have the task, return it
            return task_snapshot
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail or "Proposal cannot be accepted.",
        )
    if task_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create task from proposal.",
        )
    return task_snapshot


@router.post("/{proposal_id}/reject", response_model=ProposalSnapshot)
def reject_proposal(
    proposal_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> ProposalSnapshot:
    """Reject a proposal."""
    result_status, proposal_snapshot, detail = task_service.reject_proposal(proposal_id)
    if result_status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail or f"Proposal '{proposal_id}' was not found.",
        )
    if result_status == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail or "Proposal cannot be rejected.",
        )
    if proposal_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reject proposal.",
        )
    return proposal_snapshot
