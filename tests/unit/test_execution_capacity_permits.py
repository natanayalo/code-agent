"""Regression coverage for system-wide fan-out capacity permits."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.models  # noqa: F401
from db.base import Base
from repositories.sqlalchemy_capacity import ExecutionCapacityPermitRepository


def test_execution_capacity_permits_bound_one_queue_to_two_active_leases() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory.begin() as session:
        permits = ExecutionCapacityPermitRepository(session)
        assert permits.claim(queue_name="code-agent-codex", owner="node-a")
        assert permits.claim(queue_name="code-agent-codex", owner="node-b")
        assert not permits.claim(queue_name="code-agent-codex", owner="node-c")
        permits.release(owner="node-a")
        assert permits.claim(queue_name="code-agent-codex", owner="node-c")
