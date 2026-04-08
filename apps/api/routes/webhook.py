"""Generic webhook adapter — translates arbitrary JSON payloads into task submissions."""

from __future__ import annotations

import uuid
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

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # --- task identity fields ---
    task_text: str = Field(min_length=1, max_length=10_000)
    repo_url: str | None = Field(default=None, max_length=2048)
    branch: str | None = Field(default=None, max_length=255)
    priority: int = Field(default=0, ge=0)
    worker_override: WorkerType | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)

    # --- caller / session identity ---
    source: str = Field(default="webhook", min_length=1, max_length=100)
    # external_user_id is stored as "webhook:{source}:{external_user_id}" (max 255
    # chars in the DB).  With "webhook:" (8) + source (≤100) + ":" (1) = ≤109 chars
    # of fixed overhead, the raw caller-supplied value is capped at 255-109 = 146.
    external_user_id: str | None = Field(default=None, max_length=146)
    external_thread_id: str | None = Field(default=None, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)


def _to_task_submission(payload: WebhookPayload) -> TaskSubmission:
    """Map a generic webhook payload onto the canonical TaskSubmission model."""
    # Always prefix the channel with "webhook:" so webhook-sourced sessions are
    # never confused with native integrations (e.g. "telegram" vs "webhook:telegram").
    channel = f"webhook:{payload.source}"

    # Namespace caller-supplied IDs with "webhook:{source}:" so they remain
    # isolated from identically-named users/threads in other adapters (the
    # UserRepository lookup is global with no channel scoping).  Fall back to
    # unique UUIDs for fully anonymous calls so each request gets its own
    # isolated User and Session records.
    external_user_id = (
        f"webhook:{payload.source}:{payload.external_user_id}"
        if payload.external_user_id
        else f"webhook:anon-{uuid.uuid4().hex}"
    )
    external_thread_id = payload.external_thread_id or str(uuid.uuid4())

    session = SubmissionSession(
        channel=channel,
        external_user_id=external_user_id,
        external_thread_id=external_thread_id,
        display_name=payload.display_name,
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
