"""Health and readiness routes for local service verification."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Report that the API process is up."""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict[str, str]:
    """Report that the bootstrap service is ready to receive requests."""
    return {"status": "ready"}
