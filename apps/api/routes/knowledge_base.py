"""Knowledge-base routes for skeptical memory management (T-144)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apps.api.dependencies import get_task_service, require_any_valid_auth
from db.enums import MemoryProposalCategory, MemoryProposalStatus
from orchestrator.execution import (
    PersonalMemorySnapshot,
    PersonalMemoryUpsertRequest,
    ProjectMemorySnapshot,
    ProjectMemoryUpsertRequest,
    TaskExecutionService,
)
from orchestrator.execution_types import (
    KnowledgeBaseStatsSnapshot,
    MemoryAdmissionDecisionSnapshot,
    MemoryObservationSnapshot,
    MemoryProposalCreateRequest,
    MemoryProposalSnapshot,
)

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


@router.get("/memory-proposals", response_model=list[MemoryProposalSnapshot])
def list_memory_proposals(
    status_filter: list[MemoryProposalStatus] | None = Query(None, alias="status"),
    category: MemoryProposalCategory | None = None,
    repo_url: str | None = Query(default=None, min_length=1),
    task_id: str | None = Query(default=None, min_length=1),
    session_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[MemoryProposalSnapshot]:
    """List reviewable memory proposals with optional dashboard filters."""
    return task_service.list_memory_proposals(
        status=status_filter,
        category=category,
        repo_url=repo_url,
        task_id=task_id,
        session_id=session_id,
        limit=limit,
        offset=offset,
    )


@router.get("/observations", response_model=list[MemoryObservationSnapshot])
def list_memory_observations(
    repo_url: str | None = Query(default=None, min_length=1),
    task_id: str | None = Query(default=None, min_length=1),
    session_id: str | None = Query(default=None, min_length=1),
    source: str | None = Query(default=None, min_length=1),
    event_type: str | None = Query(default=None, min_length=1),
    admission_status: str | None = Query(default=None, min_length=1),
    q: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[MemoryObservationSnapshot]:
    """List episodic observations with optional operator filters."""
    return task_service.list_memory_observations(
        repo_url=repo_url,
        task_id=task_id,
        session_id=session_id,
        source=source,
        event_type=event_type,
        admission_status=admission_status,
        query=q,
        limit=limit,
        offset=offset,
    )


@router.get("/observations/{observation_id}", response_model=MemoryObservationSnapshot)
def get_memory_observation(
    observation_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> MemoryObservationSnapshot:
    """Fetch one observation with lineage details."""
    observation = task_service.get_memory_observation(observation_id)
    if observation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Memory observation '{observation_id}' was not found.",
        )
    return observation


@router.get("/admission-decisions", response_model=list[MemoryAdmissionDecisionSnapshot])
def list_memory_admission_decisions(
    repo_url: str | None = Query(default=None, min_length=1),
    task_id: str | None = Query(default=None, min_length=1),
    session_id: str | None = Query(default=None, min_length=1),
    decision: str | None = Query(default=None, min_length=1),
    source_observation_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[MemoryAdmissionDecisionSnapshot]:
    """List inspectable memory-admission decisions with lineage filters."""
    return task_service.list_memory_admission_decisions(
        repo_url=repo_url,
        task_id=task_id,
        session_id=session_id,
        decision=decision,
        source_observation_id=source_observation_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/memory-proposals",
    response_model=MemoryProposalSnapshot,
    status_code=status.HTTP_201_CREATED,
)
def create_memory_proposal(
    payload: MemoryProposalCreateRequest,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> MemoryProposalSnapshot:
    """Create a manual memory proposal for operator review."""
    return task_service.create_memory_proposal(payload)


@router.post("/memory-proposals/{proposal_id}/accept", response_model=MemoryProposalSnapshot)
def accept_memory_proposal(
    proposal_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> MemoryProposalSnapshot:
    """Accept a memory proposal and upsert it into durable skeptical memory."""
    result_status, proposal, detail = task_service.accept_memory_proposal(proposal_id)
    if result_status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail or f"Memory proposal '{proposal_id}' was not found.",
        )
    if result_status == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail or "Memory proposal cannot be accepted.",
        )
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to accept memory proposal.",
        )
    return proposal


@router.post("/memory-proposals/{proposal_id}/reject", response_model=MemoryProposalSnapshot)
def reject_memory_proposal(
    proposal_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> MemoryProposalSnapshot:
    """Reject a memory proposal without creating memory."""
    result_status, proposal, detail = task_service.reject_memory_proposal(proposal_id)
    if result_status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail or f"Memory proposal '{proposal_id}' was not found.",
        )
    if result_status == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail or "Memory proposal cannot be rejected.",
        )
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reject memory proposal.",
        )
    return proposal


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
