"""Shared transaction locks for readiness state and delivery coordination."""

from sqlalchemy import text
from sqlalchemy.orm import Session


def lock_readiness_alert_updates(session: Session) -> None:
    """Serialize readiness mutations on PostgreSQL for acknowledgment consistency."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(731004219)"))
