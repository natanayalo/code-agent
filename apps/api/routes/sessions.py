"""Session listing and detailed view routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apps.api.dependencies import get_task_service, require_api_auth
from orchestrator.execution import (
    SessionSnapshot,
    TaskExecutionService,
)

router = APIRouter(prefix="/sessions", tags=["sessions"], dependencies=[Depends(require_api_auth)])


@router.get("", response_model=list[SessionSnapshot])
def list_sessions(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[SessionSnapshot]:
    """List sessions with pagination."""
    return task_service.list_sessions(limit=limit, offset=offset)


@router.get("/{session_id}", response_model=SessionSnapshot)
def get_session(
    session_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> SessionSnapshot:
    """Return the latest persisted state for a session."""
    session_snapshot = task_service.get_session(session_id)
    if session_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' was not found.",
        )
    return session_snapshot
