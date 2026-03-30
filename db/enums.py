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


def enum_values(enum_class: type[StrEnum]) -> tuple[str, ...]:
    """Return the persisted string values for a StrEnum class."""

    return tuple(member.value for member in enum_class)


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


SESSION_STATUS_VALUES = enum_values(SessionStatus)
TASK_STATUS_VALUES = enum_values(TaskStatus)
WORKER_TYPE_VALUES = enum_values(WorkerType)
WORKER_RUN_STATUS_VALUES = enum_values(WorkerRunStatus)
ARTIFACT_TYPE_VALUES = enum_values(ArtifactType)
