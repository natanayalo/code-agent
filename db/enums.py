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


class HumanInteractionType(StrEnum):
    """Allowed categories of human interactions."""

    CLARIFICATION = "clarification"
    PERMISSION = "permission"
    REVIEW = "review"
    MERGE = "merge"
    BLOCKED_HELP = "blocked_help"


class HumanInteractionStatus(StrEnum):
    """Allowed lifecycle states for human interactions."""

    PENDING = "pending"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class WorkerType(StrEnum):
    """Supported worker identifiers stored in persistence."""

    GEMINI = "gemini"
    CODEX = "codex"
    OPENROUTER = "openrouter"


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
    REVIEW_RESULT = "review_result"
    INDEPENDENT_REVIEW_RESULT = "independent_review_result"


class TimelineEventType(StrEnum):
    """Allowed categories for granular task timeline events."""

    TASK_INGESTED = "task_ingested"
    TASK_CLASSIFIED = "task_classified"
    TASK_PLANNED = "task_planned"
    TASK_SPEC_GENERATED = "task_spec_generated"
    MEMORY_LOADED = "memory_loaded"
    WORKER_SELECTED = "worker_selected"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    WORKER_DISPATCHED = "worker_dispatched"
    WORKER_COMPLETED = "worker_completed"
    WORKER_FAILED = "worker_failed"
    WORKER_ERROR = "worker_error"
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_COMPLETED = "verification_completed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"


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
