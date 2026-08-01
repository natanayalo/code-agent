"""Health and readiness routes for local service verification."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

from db.base import utc_now
from orchestrator.execution import TaskExecutionService
from orchestrator.operational_health_types import ReadinessComponent, ReadinessSnapshot


class StatusResponse(BaseModel):
    """Simple status payload for service verification endpoints."""

    status: str


router = APIRouter()


@router.get("/health", response_model=StatusResponse)
def health() -> StatusResponse:
    """Report that the API process is up."""
    return StatusResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessSnapshot,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessSnapshot}},
)
def ready(request: Request, response: Response) -> ReadinessSnapshot:
    """Report whether all execution-blocking dependencies are ready."""
    task_service = getattr(request.app.state, "task_service", None)
    if not isinstance(task_service, TaskExecutionService):
        reason = "task_service_unconfigured"
        snapshot = ReadinessSnapshot(
            status="not_ready",
            checked_at=utc_now(),
            components={
                name: ReadinessComponent(status="unknown", reasons=[reason])
                for name in ("postgres", "temporal", "worker", "dispatcher")
            },
            degraded_reasons=[reason],
        )
    else:
        snapshot = task_service.get_readiness()
    response.headers["Cache-Control"] = "no-store"
    if snapshot.status != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return snapshot
