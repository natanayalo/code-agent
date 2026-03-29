"""Health and readiness routes for local service verification."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel


class StatusResponse(BaseModel):
    """Simple status payload for service verification endpoints."""

    status: str


router = APIRouter()


@router.get("/health", response_model=StatusResponse)
def health() -> StatusResponse:
    """Report that the API process is up."""
    return StatusResponse(status="ok")


@router.get("/ready", response_model=StatusResponse)
def ready() -> StatusResponse:
    """Report that the bootstrap service is ready to receive requests."""
    return StatusResponse(status="ready")
