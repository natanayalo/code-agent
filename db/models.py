"""Initial ORM models for the persistence layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from db.enums import (
    ArtifactType,
    SessionStatus,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerType,
    build_sql_enum,
)

SESSION_STATUS_ENUM = build_sql_enum(SessionStatus, name="session_status")
TASK_STATUS_ENUM = build_sql_enum(TaskStatus, name="task_status")
WORKER_TYPE_ENUM = build_sql_enum(WorkerType, name="worker_type")
WORKER_RUN_STATUS_ENUM = build_sql_enum(WorkerRunStatus, name="worker_run_status")
ARTIFACT_TYPE_ENUM = build_sql_enum(ArtifactType, name="artifact_type")
TIMELINE_EVENT_TYPE_ENUM = build_sql_enum(TimelineEventType, name="timeline_event_type")


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A known user who can own sessions and personal memory."""

    __tablename__ = "users"

    external_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sessions: Mapped[list[Session]] = relationship(back_populates="user")
    personal_memories: Mapped[list[PersonalMemory]] = relationship(back_populates="user")


class Session(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An ongoing conversation or thread."""

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "external_thread_id",
            name="uq_sessions_channel_external_thread_id",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    external_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    active_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        SESSION_STATUS_ENUM,
        nullable=False,
        default=SessionStatus.ACTIVE,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")
    tasks: Mapped[list[Task]] = relationship(back_populates="session")
    worker_runs: Mapped[list[WorkerRun]] = relationship(back_populates="session")
    session_state: Mapped[SessionState | None] = relationship(back_populates="session")

    @validates("status")
    def _coerce_status(self, _key: str, value: SessionStatus | str) -> SessionStatus:
        """Normalize assigned session statuses to the canonical enum."""

        return SessionStatus(value)


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A requested unit of work within a session."""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_created_at", "created_at"),)

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    worker_override: Mapped[WorkerType | None] = mapped_column(WORKER_TYPE_ENUM, nullable=True)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[TaskStatus] = mapped_column(
        TASK_STATUS_ENUM,
        nullable=False,
        default=TaskStatus.PENDING,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chosen_worker: Mapped[WorkerType | None] = mapped_column(WORKER_TYPE_ENUM, nullable=True)
    route_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    session: Mapped[Session] = relationship(back_populates="tasks")
    worker_runs: Mapped[list[WorkerRun]] = relationship(back_populates="task")
    inbound_deliveries: Mapped[list[InboundDelivery]] = relationship(back_populates="task")
    timeline_events: Mapped[list[TaskTimelineEvent]] = relationship(
        back_populates="task",
        order_by="TaskTimelineEvent.attempt_number.asc(), TaskTimelineEvent.sequence_number.asc()",
    )

    @validates("status")
    def _coerce_status(self, _key: str, value: TaskStatus | str) -> TaskStatus:
        """Normalize assigned task statuses to the canonical enum."""

        return TaskStatus(value)

    @validates("chosen_worker")
    def _coerce_chosen_worker(
        self,
        _key: str,
        value: WorkerType | str | None,
    ) -> WorkerType | None:
        """Normalize assigned chosen workers to the canonical enum."""

        if value is None:
            return None
        return WorkerType(value)

    @validates("worker_override")
    def _coerce_worker_override(
        self,
        _key: str,
        value: WorkerType | str | None,
    ) -> WorkerType | None:
        """Normalize assigned worker overrides to the canonical enum vocabulary."""
        if value is None:
            return None
        return WorkerType(value)


class WorkerRun(UUIDPrimaryKeyMixin, Base):
    """A single worker execution attempt for a task."""

    __tablename__ = "worker_runs"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    worker_type: Mapped[WorkerType] = mapped_column(WORKER_TYPE_ENUM, nullable=False, index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    status: Mapped[WorkerRunStatus] = mapped_column(WORKER_RUN_STATUS_ENUM, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_permission: Mapped[str | None] = mapped_column(String(64), nullable=True)
    budget_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verifier_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    commands_run: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    files_changed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_changed: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    artifact_index: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    task: Mapped[Task] = relationship(back_populates="worker_runs")
    session: Mapped[Session | None] = relationship(back_populates="worker_runs")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="worker_run")

    @validates("worker_type")
    def _coerce_worker_type(self, _key: str, value: WorkerType | str) -> WorkerType:
        """Normalize assigned worker types to the canonical enum."""

        return WorkerType(value)

    @validates("status")
    def _coerce_status(
        self,
        _key: str,
        value: WorkerRunStatus | str,
    ) -> WorkerRunStatus:
        """Normalize assigned worker-run statuses to the canonical enum."""

        return WorkerRunStatus(value)


class Artifact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An artifact emitted during a worker run."""

    __tablename__ = "artifacts"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("worker_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[ArtifactType] = mapped_column(ARTIFACT_TYPE_ENUM, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    artifact_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    worker_run: Mapped[WorkerRun] = relationship(back_populates="artifacts")

    @validates("artifact_type")
    def _coerce_artifact_type(
        self,
        _key: str,
        value: ArtifactType | str,
    ) -> ArtifactType:
        """Normalize assigned artifact types to the canonical enum."""

        return ArtifactType(value)


class InboundDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A dedupe record for externally delivered webhook events."""

    __tablename__ = "inbound_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "delivery_id",
            name="uq_inbound_deliveries_channel_delivery_id",
        ),
    )

    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    task: Mapped[Task | None] = relationship(back_populates="inbound_deliveries")


class PersonalMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Structured user-scoped memory entries."""

    __tablename__ = "memory_personal"
    __table_args__ = (
        UniqueConstraint("user_id", "memory_key", name="uq_memory_personal_user_key"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    memory_key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Skepticism metadata (T-060)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requires_verification: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped[User] = relationship(back_populates="personal_memories")


class ProjectMemory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Structured repository-scoped memory entries."""

    __tablename__ = "memory_project"
    __table_args__ = (
        UniqueConstraint("repo_url", "memory_key", name="uq_memory_project_repo_key"),
    )

    repo_url: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    memory_key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Skepticism metadata (T-060)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requires_verification: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SessionState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Compact session working state (T-061)."""

    __tablename__ = "session_states"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    active_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    decisions_made: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    identified_risks: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    files_touched: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    session: Mapped[Session] = relationship(back_populates="session_state")


class TaskTimelineEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A granular event in a task's lifecycle (T-090)."""

    __tablename__ = "task_timeline_events"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    sequence_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    event_type: Mapped[TimelineEventType] = mapped_column(
        TIMELINE_EVENT_TYPE_ENUM,
        nullable=False,
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="timeline_events")

    @validates("event_type")
    def _coerce_event_type(self, _key: str, value: TimelineEventType | str) -> TimelineEventType:
        """Normalize assigned event types to the canonical enum."""

        return TimelineEventType(value)
