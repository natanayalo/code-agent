"""Shared helpers for SQLAlchemy-backed repository implementations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, cast

from db.models import PersonalMemory, ProjectMemory

UNSET: Final = object()


def apply_memory_metadata(
    memory_entry: PersonalMemory | ProjectMemory,
    *,
    value: dict[str, Any],
    source: str | None | object = UNSET,
    confidence: float | object = UNSET,
    scope: str | None | object = UNSET,
    last_verified_at: datetime | None | object = UNSET,
    requires_verification: bool | object = UNSET,
) -> None:
    """Apply the shared skeptical-memory metadata fields to a memory entry."""

    memory_entry.value = value
    if source is not UNSET:
        memory_entry.source = cast(str | None, source)
    if confidence is not UNSET:
        memory_entry.confidence = cast(float, confidence)
    if scope is not UNSET:
        memory_entry.scope = cast(str | None, scope)
    if last_verified_at is not UNSET:
        memory_entry.last_verified_at = cast(datetime | None, last_verified_at)
    if requires_verification is not UNSET:
        memory_entry.requires_verification = cast(bool, requires_verification)
