"""Authenticated inspection endpoints for readiness alert state and delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from apps.api.dependencies import require_any_valid_auth
from db.readiness_alert_models import ReadinessAlertAcknowledgment
from db.webhook_alert_models import WebhookAlertDelivery
from orchestrator.provider_observations import ACTIONS, alert_identity
from repositories import ReadinessAlertRepository, session_scope
from repositories.sqlalchemy_readiness_alerts import (
    INTERACTION_REASON_PREFIX,
    OUTBOX_STALE_REASON_PREFIX,
    TERMINAL_RECONCILIATION_REASON_PREFIX,
    StaleReadinessIncidentError,
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
    worker_node_id: str | None = None
    provider: str | None = None
    capability: str | None = None
    next_action: str | None = None
    degraded: bool
    transition_number: int
    last_transition_at: datetime
    last_checked_at: datetime
    acknowledgment_reason: str | None = None
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None


class AcknowledgeReadinessIncidentRequest(BaseModel):
    reason: str
    transition_number: int


class ReadinessAlertAcknowledgmentResponse(BaseModel):
    reason: str
    transition_number: int
    acknowledged_at: datetime
    acknowledged_by: str


class ReadinessAlertDeliveryResponse(BaseModel):
    """Sanitized notification lifecycle metadata without message or credentials."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    channel: Literal["telegram", "webhook"] = "telegram"
    destination_id: str | None = None
    reason: str
    task_id: str | None = None
    worker_node_id: str | None = None
    provider: str | None = None
    capability: str | None = None
    next_action: str | None = None
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


def _utc_response_time(value: datetime) -> datetime:
    """Keep UTC timestamps stable when SQLite returns naive datetime values."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


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


def _public_fields(reason: str) -> dict[str, str | None]:
    identity = alert_identity(reason)
    if identity:
        worker_id, provider, capability = identity
        return {
            "reason": "provider_capability",
            "task_id": None,
            "worker_node_id": worker_id,
            "provider": provider,
            "capability": capability,
            "next_action": ACTIONS[capability],
        }
    public_reason, task_id = _public_alert_identity(reason)
    return {"reason": public_reason, "task_id": task_id}


@router.get("", response_model=list[ReadinessAlertStateResponse])
def get_readiness_alert_states(request: Request) -> list[ReadinessAlertStateResponse]:
    """Return the persisted current state for observed readiness reasons."""
    factory = _session_factory(request)
    with session_scope(factory) as session:
        states = ReadinessAlertRepository(session).list_states()
        acknowledgment_numbers = [
            (
                item.reason,
                item.transition_number if item.degraded else item.transition_number - 1,
            )
            for item in states
        ]
        acknowledgments = (
            {
                (item.reason, item.transition_number): item
                for item in session.scalars(
                    select(ReadinessAlertAcknowledgment).where(
                        tuple_(
                            ReadinessAlertAcknowledgment.reason,
                            ReadinessAlertAcknowledgment.transition_number,
                        ).in_(acknowledgment_numbers)
                    )
                ).all()
            }
            if acknowledgment_numbers
            else {}
        )
        return [
            ReadinessAlertStateResponse.model_validate(item).model_copy(
                update={
                    **_public_fields(item.reason),
                    "acknowledgment_reason": item.reason,
                    **(
                        {
                            "acknowledged_at": _utc_response_time(acknowledgment.acknowledged_at),
                            "acknowledged_by": acknowledgment.acknowledged_by,
                        }
                        if (
                            acknowledgment := acknowledgments.get(
                                (
                                    item.reason,
                                    item.transition_number
                                    if item.degraded
                                    else item.transition_number - 1,
                                )
                            )
                        )
                        else {"acknowledged_at": None, "acknowledged_by": None}
                    ),
                }
            )
            for item in states
        ]


@router.post(
    "/acknowledgments",
    response_model=ReadinessAlertAcknowledgmentResponse,
    status_code=status.HTTP_200_OK,
)
def acknowledge_readiness_incident(
    request: Request, payload: AcknowledgeReadinessIncidentRequest
) -> ReadinessAlertAcknowledgmentResponse:
    """Acknowledge an active incident without changing its observed health state."""
    factory = _session_factory(request)
    with session_scope(factory) as session:
        try:
            acknowledgment = ReadinessAlertRepository(session).acknowledge_incident(
                payload.reason,
                payload.transition_number,
                actor="operator",
            )
        except StaleReadinessIncidentError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ReadinessAlertAcknowledgmentResponse(
            reason=acknowledgment.reason,
            transition_number=acknowledgment.transition_number,
            acknowledged_at=_utc_response_time(acknowledgment.acknowledged_at),
            acknowledged_by=acknowledgment.acknowledged_by,
        )


@router.get("/deliveries", response_model=list[ReadinessAlertDeliveryResponse])
def get_readiness_alert_deliveries(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    channel: Literal["telegram", "webhook"] | None = None,
) -> list[ReadinessAlertDeliveryResponse]:
    """Return recent delivery outcomes and bounded retry metadata."""
    factory = _session_factory(request)
    with session_scope(factory) as session:
        telegram = (
            ReadinessAlertRepository(session).list_deliveries(limit=limit)
            if channel != "webhook"
            else []
        )
        webhooks = (
            list(
                session.scalars(
                    select(WebhookAlertDelivery)
                    .order_by(
                        WebhookAlertDelivery.created_at.desc(), WebhookAlertDelivery.id.desc()
                    )
                    .limit(limit)
                )
            )
            if channel != "telegram"
            else []
        )
        deliveries = [(item, "telegram") for item in telegram] + [
            (item, "webhook") for item in webhooks
        ]
        deliveries.sort(key=lambda pair: (pair[0].created_at, pair[0].id), reverse=True)
        return [
            ReadinessAlertDeliveryResponse.model_validate(item).model_copy(
                update={
                    **_public_fields(item.reason),
                    "channel": kind,
                    "destination_id": item.destination_id
                    if isinstance(item, WebhookAlertDelivery)
                    else None,
                }
            )
            for item, kind in deliveries[:limit]
        ]
