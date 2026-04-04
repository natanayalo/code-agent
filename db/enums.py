"""Canonical persisted enum vocabularies."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Enum as SQLAlchemyEnum


class SessionStatus(StrEnum):
    """Allowed lifecycle states for persisted sessions."""

    ACTIVE = "active"
    CLOSED = "closed"


class TaskStatus(StrEnum):
    """Allowed lifecycle states for persisted tasks."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerType(StrEnum):
    """Supported worker identifiers stored in persistence."""

    CLAUDE = "claude"
    CODEX = "codex"


class WorkerRunStatus(StrEnum):
    """Allowed lifecycle states for persisted worker runs."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"
    CANCELLED = "cancelled"


class ArtifactType(StrEnum):
    """Allowed categories for persisted run artifacts."""

    LOG = "log"
    DIFF = "diff"
    TEST_REPORT = "test_report"
    RESULT_SUMMARY = "result_summary"
    WORKSPACE = "workspace"


def build_sql_enum(enum_class: type[StrEnum], *, name: str) -> SQLAlchemyEnum:
    """Create a non-native SQLAlchemy enum backed by a check constraint."""

    return SQLAlchemyEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda values: [member.value for member in values],
    )
