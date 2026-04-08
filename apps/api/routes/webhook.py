"""Generic webhook adapter — translates arbitrary JSON payloads into task submissions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from apps.api.dependencies import get_task_service
from db.enums import WorkerType
from orchestrator.execution import (
    SubmissionSession,
    TaskExecutionService,
    TaskSnapshot,
    TaskSubmission,
)

router = APIRouter(prefix="/webhook", tags=["webhook"])


class WebhookPayload(BaseModel):
    """Generic inbound webhook payload.

    All fields are optional except ``task_text``.  Callers supply whatever
    subset applies; the remainder falls back to the same defaults used by the
    direct ``/tasks`` submission path.
    """

    model_config = ConfigDict(extra="forbid")

    # --- task identity fields ---
    task_text: str = Field(min_length=1)
    repo_url: str | None = None
    branch: str | None = None
    priority: int = Field(default=0, ge=0)
    worker_override: WorkerType | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)

    # --- caller / session identity ---
    source: str = Field(default="webhook", min_length=1)
    external_user_id: str | None = None
    external_thread_id: str | None = None


def _to_task_submission(payload: WebhookPayload) -> TaskSubmission:
    """Map a generic webhook payload onto the canonical TaskSubmission model."""
    channel = payload.source
    external_user_id = payload.external_user_id or f"{channel}:anonymous"
    external_thread_id = payload.external_thread_id or f"{channel}-default"

    session = SubmissionSession(
        channel=channel,
        external_user_id=external_user_id,
        external_thread_id=external_thread_id,
    )
    return TaskSubmission(
        task_text=payload.task_text,
        repo_url=payload.repo_url,
        branch=payload.branch,
        priority=payload.priority,
        worker_override=payload.worker_override,
        constraints=payload.constraints,
        budget=payload.budget,
        session=session,
    )


@router.post("", response_model=TaskSnapshot, status_code=status.HTTP_202_ACCEPTED)
def receive_webhook(
    payload: WebhookPayload,
    background_tasks: BackgroundTasks,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Accept a generic JSON webhook and enqueue it as a task.

    This endpoint is intentionally thin: it forwards the translated submission
    to the same ``TaskExecutionService`` used by the direct ``/tasks`` path so
    all execution, persistence, and observability behaviour is shared.
    """
    submission = _to_task_submission(payload)
    task_snapshot, persisted = task_service.create_task(submission)
    background_tasks.add_task(task_service.submit_task, submission, persisted)
    return task_snapshot
