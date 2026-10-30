"""Unit coverage for stale-outbox observation eligibility."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.pool import StaticPool

from apps.api.readiness_alerts import format_readiness_alert
from apps.api.routes.readiness_alerts import _public_alert_identity
from db.base import Base
from db.models import TemporalCommand
from repositories import (
    SessionRepository,
    TaskRepository,
    UserRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from repositories.sqlalchemy_operational_health import OperationalHealthRepository
from repositories.sqlalchemy_readiness_alerts import TERMINAL_RECONCILIATION_REASON_PREFIX


def _command(
    task_id: str, sequence: int, key: str, now: datetime, **fields: Any
) -> TemporalCommand:
    return TemporalCommand(
        task_id=task_id,
        command_type="signal" if sequence > 1 else "start",
        sequence_number=sequence,
        command_key=key,
        payload={},
        created_at=fields.pop("created_at", now - timedelta(seconds=61)),
        next_attempt_at=fields.pop("next_attempt_at", now),
        **fields,
    )


def test_stale_outbox_task_ids_require_current_eligibility_and_strict_age() -> None:
    """Only due, unclaimed, unresolved commands older than 60 seconds qualify."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="stale-outbox-unit")
        conversation = SessionRepository(session).create(
            user_id=user.id, channel="test", external_thread_id="stale-outbox-unit"
        )
        tasks = [
            TaskRepository(session).create(session_id=conversation.id, task_text="task")
            for _ in range(2)
        ]
        stale_task, excluded_task = tasks
        stale_task_id = stale_task.id
        stale = now - timedelta(seconds=61)
        boundary = now - timedelta(seconds=60)
        session.add_all(
            [
                _command(stale_task.id, 1, "eligible-a", now),
                _command(stale_task.id, 2, "eligible-b", now),
                _command(excluded_task.id, 1, "boundary", now, created_at=boundary),
                _command(
                    excluded_task.id,
                    2,
                    "future",
                    now,
                    created_at=stale,
                    next_attempt_at=now + timedelta(seconds=1),
                ),
                _command(excluded_task.id, 3, "dead-lettered", now, dead_lettered_at=stale),
                _command(excluded_task.id, 4, "delivered", now, delivered_at=stale),
                _command(excluded_task.id, 5, "superseded", now, superseded_at=stale),
                _command(
                    excluded_task.id,
                    6,
                    "claimed",
                    now,
                    claim_token="active",
                    claim_expires_at=now + timedelta(seconds=1),
                ),
            ]
        )
        task_ids = OperationalHealthRepository(session).stale_outbox_task_ids(
            now=now, stale_seconds=60
        )

    assert task_ids == [stale_task_id]
    Base.metadata.drop_all(engine)


def test_stale_outbox_alert_format_is_bounded_and_uses_fixed_action() -> None:
    message = format_readiness_alert("outbox_command_stale|task-safe-id", "degraded")

    assert message == (
        "Execution alert degraded: outbox_command_stale\nTask: task-safe-id"
        "\nNext action: Inspect the Temporal command outbox and worker dispatcher."
    )
    assert len(message) < 200


def test_stale_outbox_alert_public_identity_exposes_stable_reason_and_task() -> None:
    assert _public_alert_identity("outbox_command_stale|task-safe-id") == (
        "outbox_command_stale",
        "task-safe-id",
    )


def test_terminal_reconciliation_alert_format_and_identity_are_safe() -> None:
    reason = f"{TERMINAL_RECONCILIATION_REASON_PREFIX}task-safe-id"

    assert format_readiness_alert(reason, "degraded") == (
        "Execution alert degraded: terminal_reconciliation_divergent\n"
        "Task: task-safe-id\n"
        "Next action: Inspect the task and its Temporal workflow."
    )
    assert _public_alert_identity(reason) == (
        "terminal_reconciliation_divergent",
        "task-safe-id",
    )
    assert "task text" not in format_readiness_alert(reason, "recovered")
