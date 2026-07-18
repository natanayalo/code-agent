"""Task submission and status routes for the vertical-slice API."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from apps.api.config import SystemConfig
from apps.api.dependencies import get_system_config, get_task_service, require_any_valid_auth
from apps.observability import (
    SESSION_ID_ATTRIBUTE,
    SPAN_KIND_AGENT,
    TASK_ID_ATTRIBUTE,
    set_current_span_attribute,
    set_span_input_output,
    start_optional_span,
    with_span_kind,
)
from db.enums import TaskStatus, WorkerType
from orchestrator.execution import (
    InteractionInboxCard,
    InteractionResponse,
    SubmissionSession,
    TaskApprovalDecision,
    TaskExecutionService,
    TaskReplayRequest,
    TaskSnapshot,
    TaskSubmission,
    TaskSubmissionValidationError,
    TaskSummarySnapshot,
    TemporalUnavailableError,
    validate_callback_url,
)


class ScoutTriggerRequest(BaseModel):
    """Payload for manually triggering a scout task."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["repo", "research", "deep"] = "repo"
    repo_key: str | None = None
    branch: str | None = None
    focus: str | None = None
    depth: Literal["shallow", "standard", "deep"] = "standard"
    max_proposals: int = Field(default=5)

    @model_validator(mode="after")
    def _normalize_and_validate(self) -> ScoutTriggerRequest:
        if self.repo_key is not None:
            self.repo_key = self.repo_key.strip() or None
        if self.branch is not None:
            self.branch = self.branch.strip() or None
        if self.focus is not None:
            self.focus = self.focus.strip() or None

        self.max_proposals = max(1, min(20, self.max_proposals))

        if self.mode == "research" and not self.focus:
            raise ValueError("Research mode requires a focus topic.")
        return self


class CreateTaskRequest(BaseModel):
    """Public HTTP payload for submitting a new task.

    Validates inputs and resolves repository references before mapping
    to the internal TaskSubmission model.
    """

    model_config = ConfigDict(extra="forbid")

    task_text: str = Field(min_length=1)
    repo_key: str | None = Field(default=None, max_length=255)
    branch: str | None = Field(default=None, max_length=255)
    priority: int = Field(default=0, ge=0)
    worker_override: WorkerType | None = None
    worker_profile_override: str | None = Field(default=None, min_length=1, max_length=255)
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    tools: list[str] | None = None
    callback_url: str | None = Field(default=None, max_length=2048)
    session: SubmissionSession | None = None

    @field_validator("callback_url")
    @classmethod
    def _validate_callback_url(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_callback_url(v)
        return v


router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_any_valid_auth)])


@router.post("", response_model=TaskSnapshot, status_code=status.HTTP_202_ACCEPTED)
def submit_task(
    payload: CreateTaskRequest,
    task_service: TaskExecutionService = Depends(get_task_service),
    config: SystemConfig = Depends(get_system_config),
) -> TaskSnapshot:
    """Create a task, enqueue it for worker pickup, and return the pollable snapshot."""
    with start_optional_span(
        tracer_name="api.tasks",
        span_name="api.tasks.submit",
        attributes=with_span_kind(SPAN_KIND_AGENT),
    ):
        set_span_input_output(input_data=payload.model_dump(exclude={"secrets"}))

        resolved_repo_url = config.resolve_repo_key(payload.repo_key)
        if payload.repo_key and not resolved_repo_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Repo key '{payload.repo_key}' is not in the allowlist.",
            )

        submission = TaskSubmission(
            task_text=payload.task_text,
            repo_url=resolved_repo_url,
            branch=payload.branch,
            priority=payload.priority,
            worker_override=payload.worker_override,
            worker_profile_override=payload.worker_profile_override,
            constraints=payload.constraints,
            budget=payload.budget,
            secrets=payload.secrets,
            tools=payload.tools,
            callback_url=payload.callback_url,
            session=payload.session
            or SubmissionSession(
                channel="http",
                external_user_id="http:anonymous",
                external_thread_id=str(uuid.uuid4()),
            ),
        )

        try:
            task_service.ensure_temporal_available()
            task_snapshot, _ = task_service.create_task(submission)
            set_current_span_attribute(TASK_ID_ATTRIBUTE, task_snapshot.task_id)
            set_current_span_attribute(SESSION_ID_ATTRIBUTE, task_snapshot.session_id)
            return task_snapshot
        except TaskSubmissionValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        except TemporalUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc


@router.post("/scout/trigger", response_model=TaskSnapshot, status_code=status.HTTP_202_ACCEPTED)
def trigger_scout_task(
    payload: ScoutTriggerRequest | None = None,
    task_service: TaskExecutionService = Depends(get_task_service),
    config: SystemConfig = Depends(get_system_config),
) -> TaskSnapshot:
    """Manually trigger a scout task using system defaults."""
    req = payload or ScoutTriggerRequest()

    branch = config.scout_branch
    if req.repo_key:
        repo_url = config.resolve_repo_key(req.repo_key)
        if not repo_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Repo key '{req.repo_key}' is not in the allowlist.",
            )
    else:
        repo_url = config.resolve_repo_key(config.scout_repo_key)

    if not repo_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Scout functionality is not fully configured "
                "(missing valid repo_key or default repo)."
            ),
        )

    if req.branch:
        branch = req.branch

    constraints = {
        "task_type": "scout",
        "trigger_source": "manual",
        "scout_mode": req.mode,
        "scout_depth": req.depth,
        "max_proposals": req.max_proposals,
    }
    if req.focus:
        constraints["scout_focus"] = req.focus

    submission = TaskSubmission(
        task_text=config.scout_task_text,
        repo_url=repo_url,
        branch=branch,
        priority=0,
        constraints=constraints,
        session=SubmissionSession(
            channel="scheduler",
            external_user_id="system:scout-scheduler",
            external_thread_id="scout-scheduler",
            display_name="Scout Scheduler",
        ),
    )

    with start_optional_span(
        tracer_name="api.tasks",
        span_name="api.tasks.scout.trigger",
        attributes=with_span_kind(SPAN_KIND_AGENT),
    ):
        set_span_input_output(input_data=submission.model_dump(exclude={"secrets"}))
        try:
            task_service.ensure_temporal_available()
            task_snapshot, _ = task_service.create_task(submission)
            set_current_span_attribute(TASK_ID_ATTRIBUTE, task_snapshot.task_id)
            set_current_span_attribute(SESSION_ID_ATTRIBUTE, task_snapshot.session_id)
            return task_snapshot
        except TaskSubmissionValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        except TemporalUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc


@router.get("", response_model=list[TaskSummarySnapshot])
def list_tasks(
    session_id: str | None = None,
    status_filter: TaskStatus | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[TaskSummarySnapshot]:
    """List tasks with optional filtering and pagination using summary views."""
    return task_service.list_tasks(
        session_id=session_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )


@router.get("/interactions/pending", response_model=list[InteractionInboxCard])
def list_pending_interactions(
    task_service: TaskExecutionService = Depends(get_task_service),
) -> list[InteractionInboxCard]:
    """List all pending interactions across tasks for the operator inbox."""
    return task_service.list_pending_interactions()


@router.get("/{task_id}", response_model=TaskSnapshot)
def get_task(
    task_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Return the latest persisted state for a submitted task."""
    task_snapshot = task_service.get_task(task_id)
    if task_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' was not found.",
        )
    return task_snapshot


@router.post("/{task_id}/approval", response_model=TaskSnapshot)
def decide_task_approval(
    task_id: str,
    payload: TaskApprovalDecision,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Apply an idempotent manual approval decision for a paused task."""
    result = task_service.apply_task_approval_decision(task_id=task_id, approved=payload.approved)
    if result.status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.detail or f"Task '{task_id}' was not found.",
        )
    if result.status == "conflict":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.detail or "Task decision conflicts with an existing approval decision.",
        )
    if result.status == "not_waiting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.detail or "Task is not awaiting approval.",
        )
    if result.task_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Task decision was applied but the task snapshot could not be reloaded.",
        )
    return result.task_snapshot


@router.post("/{task_id}/cancel", response_model=TaskSnapshot)
def cancel_task(
    task_id: str,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Terminally cancel a task and stop any in-flight worker execution."""
    task_snapshot = task_service.cancel_task(task_id=task_id)
    if task_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{task_id}' was not found.",
        )
    return task_snapshot


@router.post(
    "/{task_id}/replay",
    response_model=TaskSnapshot,
    status_code=status.HTTP_201_CREATED,
)
def replay_task(
    task_id: str,
    payload: TaskReplayRequest | None = None,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Replay a prior terminal task, creating a new task with optional overrides."""
    try:
        result = task_service.replay_task(
            source_task_id=task_id,
            replay_request=payload,
        )
    except TaskSubmissionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if result.status == "not_found":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=result.detail or f"Task '{task_id}' was not found.",
        )
    if result.status == "not_replayable":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result.detail or "Task is not in a terminal state and cannot be replayed.",
        )
    if result.task_snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Replay task was created but the snapshot could not be reloaded.",
        )
    return result.task_snapshot


@router.post("/{task_id}/interactions/{interaction_id}/response", response_model=TaskSnapshot)
def record_interaction_response(
    task_id: str,
    interaction_id: str,
    payload: InteractionResponse,
    task_service: TaskExecutionService = Depends(get_task_service),
) -> TaskSnapshot:
    """Submit a response to a pending human interaction."""
    snapshot = task_service.record_interaction_response(
        task_id=task_id,
        interaction_id=interaction_id,
        response=payload,
    )
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Interaction '{interaction_id}' for task '{task_id}' was not found.",
        )
    return snapshot
