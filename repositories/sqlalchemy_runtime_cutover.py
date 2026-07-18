"""Persistence boundary for immutable runtime cutover evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from db.models import RuntimeCutover

TEMPORAL_ONLY_CUTOVER_NAME = "temporal_only"


class RuntimeCutoverRepository:
    """Read and initialize the one-way runtime cutover record."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def temporal_only_cutover(self) -> RuntimeCutover | None:
        return self.session.get(RuntimeCutover, TEMPORAL_ONLY_CUTOVER_NAME)

    def initialize_temporal_only(self, configured_at: datetime | None) -> datetime | None:
        """Persist the first valid boundary and reject later conflicting configuration."""
        existing = self.temporal_only_cutover()
        if existing is None:
            if configured_at is None:
                return None
            self.session.add(
                RuntimeCutover(
                    cutover_name=TEMPORAL_ONLY_CUTOVER_NAME,
                    cutover_at=configured_at,
                )
            )
            return configured_at
        existing_at = (
            existing.cutover_at.replace(tzinfo=UTC)
            if existing.cutover_at.tzinfo is None
            else existing.cutover_at.astimezone(UTC)
        )
        if configured_at is not None and existing_at != configured_at:
            raise RuntimeError(
                "TEMPORAL_ONLY_CUTOVER_AT conflicts with the persisted temporal_only cutover."
            )
        return existing_at
