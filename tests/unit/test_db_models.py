"""Unit tests for the initial ORM metadata."""

from __future__ import annotations

from sqlalchemy import JSON, DateTime
from sqlalchemy import Enum as SQLAlchemyEnum

import db.models  # noqa: F401
from db.base import Base
from db.enums import (
    ArtifactType,
    SessionStatus,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerType,
)

EXPECTED_TABLES = {
    "artifacts",
    "inbound_deliveries",
    "memory_personal",
    "memory_project",
    "sessions",
    "session_states",
    "tasks",
    "task_timeline_events",
    "users",
    "worker_runs",
}


def test_model_metadata_defines_expected_tables() -> None:
    """The ORM metadata contains the initial persistence tables."""
    assert EXPECTED_TABLES == set(Base.metadata.tables)


def test_model_metadata_uses_canonical_enums_for_constrained_columns() -> None:
    """Persisted enum-like columns are backed by explicit SQLAlchemy enum types."""

    expected_columns = {
        ("sessions", "status"): SessionStatus,
        ("tasks", "status"): TaskStatus,
        ("tasks", "chosen_worker"): WorkerType,
        ("worker_runs", "worker_type"): WorkerType,
        ("worker_runs", "status"): WorkerRunStatus,
        ("artifacts", "artifact_type"): ArtifactType,
        ("task_timeline_events", "event_type"): TimelineEventType,
    }

    for (table_name, column_name), enum_class in expected_columns.items():
        column_type = Base.metadata.tables[table_name].c[column_name].type
        assert isinstance(column_type, SQLAlchemyEnum)
        assert column_type.enum_class is enum_class
        assert list(column_type.enums) == [member.value for member in enum_class]
        assert not column_type.native_enum
        assert column_type.create_constraint


def test_model_metadata_defines_retention_expiry_column_type() -> None:
    """Retention cleanup needs an explicit timestamp on worker runs."""
    column_type = Base.metadata.tables["worker_runs"].c["retention_expires_at"].type
    assert isinstance(column_type, DateTime)


def test_model_metadata_defines_task_spec_column_type() -> None:
    """TaskSpec generation needs an inspectable JSON contract on tasks."""
    column_type = Base.metadata.tables["tasks"].c["task_spec"].type
    assert isinstance(column_type, JSON)
