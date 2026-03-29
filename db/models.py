"""Initial ORM models for the persistence layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


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
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")
    tasks: Mapped[list[Task]] = relationship(back_populates="session")


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A requested unit of work within a session."""

    __tablename__ = "tasks"

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chosen_worker: Mapped[str | None] = mapped_column(String(50), nullable=True)
    route_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    session: Mapped[Session] = relationship(back_populates="tasks")
    worker_runs: Mapped[list[WorkerRun]] = relationship(back_populates="task")


class WorkerRun(UUIDPrimaryKeyMixin, Base):
    """A single worker execution attempt for a task."""

    __tablename__ = "worker_runs"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    worker_type: Mapped[str] = mapped_column(String(50), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    commands_run: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    files_changed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    artifact_index: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)

    task: Mapped[Task] = relationship(back_populates="worker_runs")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="worker_run")


class Artifact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An artifact emitted during a worker run."""

    __tablename__ = "artifacts"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("worker_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    artifact_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    worker_run: Mapped[WorkerRun] = relationship(back_populates="artifacts")


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
