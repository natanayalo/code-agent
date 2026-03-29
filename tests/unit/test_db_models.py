"""Unit tests for the initial ORM metadata."""

from __future__ import annotations

import db.models  # noqa: F401
from db.base import Base

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
