"""Persistence boundary for immutable runtime cutover evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
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
            try:
                with self.session.begin_nested():
                    self.session.add(
                        RuntimeCutover(
                            cutover_name=TEMPORAL_ONLY_CUTOVER_NAME,
                            cutover_at=configured_at,
                        )
                    )
                    self.session.flush()
            except IntegrityError:
                existing = self.temporal_only_cutover()
                if existing is None:
                    raise RuntimeError(
                        "Unable to read the persisted temporal_only cutover after a "
                        "concurrent initialization."
                    ) from None
            else:
                return configured_at
        assert existing is not None
        existing_at = self._normalized_cutover_at(existing)
        if configured_at is not None and existing_at != configured_at:
            raise RuntimeError(
                "TEMPORAL_ONLY_CUTOVER_AT conflicts with the persisted temporal_only cutover."
            )
        return existing_at

    @staticmethod
    def _normalized_cutover_at(existing: RuntimeCutover) -> datetime:
        return (
            existing.cutover_at.replace(tzinfo=UTC)
            if existing.cutover_at.tzinfo is None
            else existing.cutover_at.astimezone(UTC)
        )
