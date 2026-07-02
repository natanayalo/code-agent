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


class ExecutionPlanNodeStatus(StrEnum):
    """Allowed lifecycle states for execution plan nodes."""

    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


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


class HumanInteractionHitlMode(StrEnum):
    """Allowed Human-in-the-Loop modes for human interactions."""

    REQUIRE_APPROVAL = "require_approval"
    PROCEED_WITH_FLAG = "proceed_with_flag"
    NOTIFY_ONLY = "notify_only"


class WorkerRuntimeMode(StrEnum):
    """Supported worker runtime execution modes."""

    NATIVE_AGENT = "native_agent"
    TOOL_LOOP = "tool_loop"
    PLANNER_ONLY = "planner_only"
    REVIEWER_ONLY = "reviewer_only"
    SHELL = "shell"


class WorkerType(StrEnum):
    """Supported worker identifiers stored in persistence."""

    ANTIGRAVITY = "antigravity"
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


class WorkerNodeStatus(StrEnum):
    """Allowed lifecycle states for queue worker nodes."""

    ACTIVE = "active"
    DRAINING = "draining"
    OFFLINE = "offline"
    QUARANTINED = "quarantined"


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
    TASK_SPEC_AND_ROUTE_GENERATED = "task_spec_and_route_generated"
    MEMORY_LOADED = "memory_loaded"
    MEMORY_PERSISTED = "memory_persisted"
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
    VERIFICATION_SKIPPED = "verification_skipped"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    WORKSPACE_PROVISIONED = "workspace_provisioned"
    ENVIRONMENT_INITIALIZED = "environment_initialized"
    INFRA_FAILURE = "infra_failure"
    DELIVERY_STARTED = "delivery_started"
    DELIVERY_COMPLETED = "delivery_completed"
    DELIVERY_FAILED = "delivery_failed"


class ProposalStatus(StrEnum):
    """Allowed lifecycle states for persisted proposals."""

    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IMPLEMENTED = "implemented"


class ProposalType(StrEnum):
    """Allowed categories for persisted proposals."""

    SCOUT = "scout"
    REFLECTION = "reflection"


def coerce_worker_type(value: object) -> WorkerType:
    """Normalize worker identifiers to the canonical worker enum."""

    if isinstance(value, WorkerType):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "gemini":
            return WorkerType.ANTIGRAVITY
        return WorkerType(normalized)
    raise ValueError(f"Invalid worker type: {value!r}")


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
