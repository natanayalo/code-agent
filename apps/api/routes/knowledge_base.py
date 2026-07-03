"""Knowledge-base routes for skeptical memory management (T-144)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apps.api.dependencies import get_task_service, require_any_valid_auth
from orchestrator.execution import (
    PersonalMemorySnapshot,
    PersonalMemoryUpsertRequest,
    ProjectMemorySnapshot,
    ProjectMemoryUpsertRequest,
    TaskExecutionService,
)
from orchestrator.execution_types import KnowledgeBaseStatsSnapshot

router = APIRouter(
    prefix="/knowledge-base",
    tags=["knowledge-base"],
    dependencies=[Depends(require_any_valid_auth)],
)


@router.get("/stats", response_model=KnowledgeBaseStatsSnapshot)
def get_knowledge_base_stats(
    repo_url: str | None = Query(default=None),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> KnowledgeBaseStatsSnapshot:
    """Return exact skeptical-memory inventory counts for dashboard browse."""
    return task_service.get_knowledge_base_stats(repo_url=repo_url)


@router.get("/personal", response_model=list[PersonalMemorySnapshot])
def list_personal_memory(
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[PersonalMemorySnapshot]:
    """List operator-global personal skeptical-memory entries."""
    return task_service.list_personal_memory(limit=limit, offset=offset)


@router.get("/personal/search", response_model=list[PersonalMemorySnapshot])
def search_personal_memory(
    q: str = Query(default=""),
    limit: int = Query(20, ge=1, le=100),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[PersonalMemorySnapshot]:
    """Search operator-global personal skeptical-memory entries."""
    return task_service.search_personal_memory(query=q, limit=limit)


@router.put("/personal", response_model=PersonalMemorySnapshot)
def upsert_personal_memory(
    payload: PersonalMemoryUpsertRequest,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> PersonalMemorySnapshot:
    """Create or update one personal skeptical-memory entry."""
    return task_service.upsert_personal_memory(payload)


@router.delete("/personal", status_code=status.HTTP_204_NO_CONTENT)
def delete_personal_memory(
    memory_key: str = Query(min_length=1),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> None:
    """Delete one personal skeptical-memory entry."""
    deleted = task_service.delete_personal_memory(memory_key=memory_key)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=("Personal memory entry was not found for the supplied memory_key."),
        )


@router.get("/project", response_model=list[ProjectMemorySnapshot])
def list_project_memory(
    repo_url: str | None = Query(default=None, min_length=1),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[ProjectMemorySnapshot]:
    """List project skeptical-memory entries."""
    return task_service.list_project_memory(repo_url=repo_url, limit=limit, offset=offset)


@router.get("/project/search", response_model=list[ProjectMemorySnapshot])
def search_project_memory(
    repo_url: str = Query(min_length=1),
    q: str = Query(default=""),
    limit: int = Query(20, ge=1, le=100),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[ProjectMemorySnapshot]:
    """Search project skeptical-memory entries for one repository."""
    return task_service.search_project_memory(repo_url=repo_url, query=q, limit=limit)


@router.put("/project", response_model=ProjectMemorySnapshot)
def upsert_project_memory(
    payload: ProjectMemoryUpsertRequest,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> ProjectMemorySnapshot:
    """Create or update one project skeptical-memory entry."""
    return task_service.upsert_project_memory(payload)


@router.delete("/project", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_memory(
    repo_url: str = Query(min_length=1),
    memory_key: str = Query(min_length=1),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> None:
    """Delete one project skeptical-memory entry."""
    deleted = task_service.delete_project_memory(repo_url=repo_url, memory_key=memory_key)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=("Project memory entry was not found for the supplied repo_url and memory_key."),
        )
