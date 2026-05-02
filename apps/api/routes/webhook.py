"""Generic webhook adapter — translates arbitrary JSON payloads into task submissions."""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.api.dependencies import get_task_service, require_api_auth
from db.enums import WorkerType
from orchestrator.execution import (
    DeliveryKey,
    SubmissionSession,
    TaskExecutionService,
    TaskSnapshot,
    TaskSubmission,
    validate_callback_url,
)

router = APIRouter(prefix="/webhook", tags=["webhook"], dependencies=[Depends(require_api_auth)])
WEBHOOK_DEFAULT_REPO_URL_ENV_VAR = "CODE_AGENT_WEBHOOK_DEFAULT_REPO_URL"


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
    delivery_id: str | None = Field(default=None, min_length=1, max_length=255)
    callback_url: str | None = Field(default=None, max_length=2048)

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, value: str | None) -> str | None:
        """Reject malformed or obviously unsafe callback targets at request validation time."""
        return validate_callback_url(value)


def _to_task_submission(payload: WebhookPayload) -> TaskSubmission:
    """Map a generic webhook payload onto the canonical TaskSubmission model."""
    # Always prefix the channel with "webhook:" so webhook-sourced sessions are
    # never confused with native integrations (e.g. "telegram" vs "webhook:telegram").
    channel = f"webhook:{payload.source}"

    # Namespace caller-supplied IDs with "webhook:{source}:" so they remain
    # isolated from identically-named users/threads in other adapters (the
    # UserRepository lookup is global with no channel scoping).
    #
    # For fully anonymous calls (no external_user_id supplied) we use a stable
    # per-source sentinel ("webhook:{source}:anonymous") so all anonymous callers
    # from the same source share one User row rather than creating a new User for
    # every request.  Task isolation is still guaranteed because external_thread_id
    # falls back to a unique UUID, giving each anonymous request its own Session.
    external_user_id = (
        f"webhook:{payload.source}:{payload.external_user_id}"
        if payload.external_user_id
        else f"webhook:{payload.source}:anonymous"
    )
    external_thread_id = payload.external_thread_id or str(uuid.uuid4())

    default_repo = os.environ.get(WEBHOOK_DEFAULT_REPO_URL_ENV_VAR)
    resolved_repo_url = payload.repo_url if payload.repo_url and payload.repo_url.strip() else None
    if resolved_repo_url is None:
        resolved_repo_url = default_repo if default_repo and default_repo.strip() else None

    session = SubmissionSession(
        channel=channel,
        external_user_id=external_user_id,
        external_thread_id=external_thread_id,
        display_name=payload.display_name,
    )
    return TaskSubmission(
        task_text=payload.task_text,
        repo_url=resolved_repo_url,
        branch=payload.branch,
        priority=payload.priority,
        worker_override=payload.worker_override,
        constraints=payload.constraints,
        budget=payload.budget,
        callback_url=payload.callback_url,
        session=session,
    )


@router.post("", response_model=TaskSnapshot, status_code=status.HTTP_202_ACCEPTED)
def receive_webhook(
    payload: WebhookPayload,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Accept a generic JSON webhook and enqueue it as a task.

    This endpoint is intentionally thin: it forwards the translated submission
    to the same ``TaskExecutionService`` used by the direct ``/tasks`` path so
    all execution, persistence, and observability behaviour is shared.
    """
    from contextlib import nullcontext

    from apps.observability import set_span_input_output

    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

        tracer = otel_trace.get_tracer("api.webhook")
        span_cm = tracer.start_as_current_span("api.webhook")
    except (ImportError, Exception):
        span_cm = nullcontext()

    with span_cm:
        set_span_input_output(input_data=payload.model_dump(), kind="AGENT")

        submission = _to_task_submission(payload)

        # We will link the session ID after creating the outcome

        outcome = task_service.create_task_outcome(
            submission,
            delivery_key=(
                DeliveryKey(channel=submission.session.channel, delivery_id=payload.delivery_id)
                if payload.delivery_id is not None
                else None
            ),
        )

        try:
            from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]

            otel_trace.get_current_span().set_attribute(
                "session.id", outcome.task_snapshot.session_id
            )
        except Exception:
            pass

        set_span_input_output(
            input_data=None, output_data=outcome.task_snapshot.model_dump(), kind="AGENT"
        )

        return outcome.task_snapshot
