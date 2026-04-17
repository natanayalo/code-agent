"""Operational metrics route for the code-agent service."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from apps.api.dependencies import get_task_service, require_api_auth
from orchestrator.execution import OperationalMetrics, TaskExecutionService

router = APIRouter(prefix="/metrics", tags=["metrics"], dependencies=[Depends(require_api_auth)])


@router.get("", response_model=OperationalMetrics)
def get_metrics(
    task_service: TaskExecutionService = Depends(get_task_service),
) -> OperationalMetrics:
    """Return aggregated operational metrics for the service."""
    return task_service.get_operational_metrics()
