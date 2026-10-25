"""GitHub webhook ingress route for native issue intake and delivery control (M30)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from apps.api.config import SystemConfig
from apps.api.dependencies import (
    _check_no_raw_secrets,
    get_system_config,
    get_task_service,
    require_github_webhook_auth,
)
from apps.observability import (
    SESSION_ID_ATTRIBUTE,
    SPAN_KIND_AGENT,
    TASK_ID_ATTRIBUTE,
    set_current_span_attribute,
    set_span_input_output,
    start_optional_span,
    with_span_kind,
)
from orchestrator.execution import TaskExecutionService, TemporalUnavailableError
from orchestrator.execution_types import TaskSubmissionValidationError
from orchestrator.github_events import process_github_webhook_event

router = APIRouter(prefix="/github", tags=["github"])


class GitHubWebhookResponse(BaseModel):
    """Structured response from the GitHub webhook intake route."""

    ok: bool
    status: str
    event: str
    action: str | None = None
    delivery_id: str
    duplicate: bool = False
    task_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    detail: str | None = None


@router.post(
    "/webhook",
    response_model=GitHubWebhookResponse,
    status_code=status.HTTP_200_OK,
)
async def receive_github_webhook(
    raw_body: bytes = Depends(require_github_webhook_auth),
    x_github_event: str | None = Header(None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(None, alias="X-GitHub-Delivery"),
    task_service: TaskExecutionService = Depends(get_task_service),
    config: SystemConfig = Depends(get_system_config),
) -> GitHubWebhookResponse:
    """Accept, authenticate, and process an inbound GitHub webhook event."""
    if not x_github_event:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Event header.",
        )
    if not x_github_delivery:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-GitHub-Delivery header.",
        )

    try:
        raw_json = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        ) from exc

    if not isinstance(raw_json, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload.",
        )

    payload: dict[str, Any] = raw_json
    _check_no_raw_secrets(payload)

    span_cm = start_optional_span(
        tracer_name="api.github.webhook",
        span_name="api.github.webhook",
        attributes=with_span_kind(SPAN_KIND_AGENT),
    )
    with span_cm:
        set_span_input_output(
            input_data={"event": x_github_event, "delivery_id": x_github_delivery}
        )
        try:
            result = process_github_webhook_event(
                payload=payload,
                event=x_github_event,
                delivery_id=x_github_delivery,
                config=config,
                task_service=task_service,
            )
        except TaskSubmissionValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        except TemporalUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

        if result.task_id:
            set_current_span_attribute(TASK_ID_ATTRIBUTE, result.task_id)
        if result.session_id:
            set_current_span_attribute(SESSION_ID_ATTRIBUTE, result.session_id)
        set_span_input_output(input_data=None, output_data=result.model_dump())

        return GitHubWebhookResponse.model_validate(result.model_dump())
