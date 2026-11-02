"""Lease claiming preserves active attempts and permits the last allowed retry."""

from datetime import timedelta

from db.base import utc_now
from repositories import ReadinessAlertRepository, session_scope
from tests.integration import conftest as integration_fixtures

session_factory = integration_fixtures.session_factory


def test_active_final_lease_is_not_expired_early(session_factory):
    with session_scope(session_factory) as session:
        repo = ReadinessAlertRepository(session)
        repo.record_snapshot(["worker_unavailable"])
        item = repo.claim_due(limit=1)[0]
        item.attempts = 8
        item.lease_expires_at = utc_now() + timedelta(seconds=60)
        assert repo.claim_due(limit=1) == []
        assert item.status == "processing" and item.attempts == 8


def test_expired_lease_below_budget_gets_final_attempt(session_factory):
    with session_scope(session_factory) as session:
        repo = ReadinessAlertRepository(session)
        repo.record_snapshot(["worker_unavailable"])
        item = repo.claim_due(limit=1)[0]
        item.attempts = 7
        item.lease_expires_at = utc_now() - timedelta(seconds=1)
        claimed = repo.claim_due(limit=1)
        assert claimed == [item]
        assert item.attempts == 8 and item.status == "processing"
