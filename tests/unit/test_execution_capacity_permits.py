"""Regression coverage for system-wide fan-out capacity permits."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.models  # noqa: F401
from db.base import Base, utc_now
from db.models import ExecutionCapacityPermit
from repositories.sqlalchemy_capacity import ExecutionCapacityPermitRepository


def test_execution_capacity_permits_bound_one_queue_to_two_active_leases() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory.begin() as session:
        permits = ExecutionCapacityPermitRepository(session)
        assert permits.claim(queue_name="code-agent-codex", owner="node-a", token="a")
        assert permits.claim(queue_name="code-agent-codex", owner="node-b", token="b")
        assert not permits.claim(queue_name="code-agent-codex", owner="node-c", token="c")
        assert permits.release(owner="node-a", token="a")
        assert permits.claim(queue_name="code-agent-codex", owner="node-c", token="c")


def test_execution_capacity_permit_fences_duplicate_attempts_and_renews_lease() -> None:
    """A retried logical activity cannot release or outlive the original lease."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory.begin() as first_session:
        first = ExecutionCapacityPermitRepository(first_session)
        assert first.claim(queue_name="code-agent-codex", owner="node-a", token="attempt-a")

    # A separate session models another Temporal activity attempt/process.
    with session_factory.begin() as retry_session:
        retry = ExecutionCapacityPermitRepository(retry_session)
        assert not retry.claim(queue_name="code-agent-codex", owner="node-a", token="attempt-b")
        assert not retry.release(owner="node-a", token="attempt-b")

    with session_factory.begin() as owner_session:
        owner = ExecutionCapacityPermitRepository(owner_session)
        assert owner.heartbeat(owner="node-a", token="attempt-a")
        assert owner.release(owner="node-a", token="attempt-a")


def test_execution_capacity_permit_does_not_renew_after_expiry() -> None:
    """An expired lease must be reclaimed rather than resurrected by its owner."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory.begin() as session:
        permits = ExecutionCapacityPermitRepository(session)
        assert permits.claim(queue_name="code-agent-codex", owner="node-a", token="attempt-a")
        permit = session.query(ExecutionCapacityPermit).filter_by(lease_owner="node-a").one()
        permit.lease_expires_at = utc_now() - timedelta(seconds=1)
        assert not permits.heartbeat(owner="node-a", token="attempt-a")
