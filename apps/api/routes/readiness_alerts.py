"""Authenticated inspection endpoints for readiness alert state and delivery."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from apps.api.dependencies import require_any_valid_auth
from repositories import ReadinessAlertRepository, session_scope
from repositories.sqlalchemy_readiness_alerts import (
    INTERACTION_REASON_PREFIX,
    OUTBOX_STALE_REASON_PREFIX,
    TERMINAL_RECONCILIATION_REASON_PREFIX,
)

router = APIRouter(
    prefix="/alerts/readiness",
    tags=["readiness alerts"],
    dependencies=[Depends(require_any_valid_auth)],
)


class ReadinessAlertStateResponse(BaseModel):
    """Latest stored status for one stable readiness reason."""

    model_config = ConfigDict(from_attributes=True)

    reason: str
    task_id: str | None = None
    degraded: bool
    transition_number: int
    last_transition_at: datetime
    last_checked_at: datetime


class ReadinessAlertDeliveryResponse(BaseModel):
    """Sanitized notification lifecycle metadata without message or credentials."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    reason: str
    task_id: str | None = None
    transition_number: int
    transition: str
    status: str
    attempts: int
    next_attempt_at: datetime
    delivered_at: datetime | None
    last_failure_code: str | None
    last_http_status: int | None
    created_at: datetime


def _session_factory(request: Request) -> sessionmaker[Session]:
    service = getattr(request.app.state, "task_service", None)
    factory = getattr(service, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Readiness alert persistence is not configured.",
        )
    return cast(sessionmaker[Session], factory)


def _public_alert_identity(reason: str) -> tuple[str, str | None]:
    if reason.startswith(INTERACTION_REASON_PREFIX):
        return "interaction_wait_stuck", reason.removeprefix(INTERACTION_REASON_PREFIX)
    if reason.startswith(OUTBOX_STALE_REASON_PREFIX):
        return "outbox_command_stale", reason.removeprefix(OUTBOX_STALE_REASON_PREFIX)
    if reason.startswith(TERMINAL_RECONCILIATION_REASON_PREFIX):
        return (
            "terminal_reconciliation_divergent",
            reason.removeprefix(TERMINAL_RECONCILIATION_REASON_PREFIX),
        )
    return reason, None


@router.get("", response_model=list[ReadinessAlertStateResponse])
def get_readiness_alert_states(request: Request) -> list[ReadinessAlertStateResponse]:
    """Return the persisted current state for observed readiness reasons."""
    factory = _session_factory(request)
    with session_scope(factory) as session:
        states = ReadinessAlertRepository(session).list_states()
        return [
            ReadinessAlertStateResponse.model_validate(item).model_copy(
                update={
                    "reason": _public_alert_identity(item.reason)[0],
                    "task_id": _public_alert_identity(item.reason)[1],
                }
            )
            for item in states
        ]


@router.get("/deliveries", response_model=list[ReadinessAlertDeliveryResponse])
def get_readiness_alert_deliveries(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ReadinessAlertDeliveryResponse]:
    """Return recent delivery outcomes and bounded retry metadata."""
    factory = _session_factory(request)
    with session_scope(factory) as session:
        deliveries = ReadinessAlertRepository(session).list_deliveries(limit=limit)
        return [
            ReadinessAlertDeliveryResponse.model_validate(item).model_copy(
                update={
                    "reason": _public_alert_identity(item.reason)[0],
                    "task_id": _public_alert_identity(item.reason)[1],
                }
            )
            for item in deliveries
        ]
