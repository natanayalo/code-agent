"""Unit tests for the initial ORM metadata."""

from __future__ import annotations

from sqlalchemy import Enum as SQLAlchemyEnum

import db.models  # noqa: F401
from db.base import Base
from db.enums import ArtifactType, SessionStatus, TaskStatus, WorkerRunStatus, WorkerType

EXPECTED_TABLES = {
    "artifacts",
    "memory_personal",
    "memory_project",
    "sessions",
    "tasks",
    "users",
    "worker_runs",
}


def test_model_metadata_defines_expected_tables() -> None:
    """The ORM metadata contains the initial persistence tables."""
    assert EXPECTED_TABLES == set(Base.metadata.tables)


def test_model_metadata_uses_canonical_enums_for_constrained_columns() -> None:
    """Persisted enum-like columns are backed by explicit SQLAlchemy enum types."""

    expected_columns = {
        ("sessions", "status"): (SessionStatus, ["active", "closed"]),
        (
            "tasks",
            "status",
        ): (TaskStatus, ["pending", "in_progress", "completed", "failed", "cancelled"]),
        ("tasks", "chosen_worker"): (WorkerType, ["claude", "codex"]),
        ("worker_runs", "worker_type"): (WorkerType, ["claude", "codex"]),
        (
            "worker_runs",
            "status",
        ): (WorkerRunStatus, ["queued", "running", "success", "failure", "error", "cancelled"]),
        (
            "artifacts",
            "artifact_type",
        ): (ArtifactType, ["log", "diff", "test_report", "result_summary"]),
    }

    for (table_name, column_name), (enum_class, expected_values) in expected_columns.items():
        column_type = Base.metadata.tables[table_name].c[column_name].type
        assert isinstance(column_type, SQLAlchemyEnum)
        assert column_type.enum_class is enum_class
        assert list(column_type.enums) == expected_values
        assert not column_type.native_enum
        assert column_type.create_constraint
