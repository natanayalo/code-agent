from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now
from db.enums import (
    ArtifactType,
    ExecutionPlanNodeStatus,
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    MemoryProposalCategory,
    MemoryProposalStatus,
    OrchestrationRuntime,
    ProposalStatus,
    ProposalType,
    SessionStatus,
    TaskStatus,
    TimelineEventType,
    WorkerNodeStatus,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
    build_sql_enum,
    coerce_worker_type,
)

logger = logging.getLogger(__name__)

SESSION_STATUS_ENUM = build_sql_enum(SessionStatus, name="session_status")
TASK_STATUS_ENUM = build_sql_enum(TaskStatus, name="task_status")
WORKER_TYPE_ENUM = build_sql_enum(WorkerType, name="worker_type")
WORKER_RUN_STATUS_ENUM = build_sql_enum(WorkerRunStatus, name="worker_run_status")
WORKER_NODE_STATUS_ENUM = build_sql_enum(WorkerNodeStatus, name="worker_node_status")
ARTIFACT_TYPE_ENUM = build_sql_enum(ArtifactType, name="artifact_type")
TIMELINE_EVENT_TYPE_ENUM = build_sql_enum(TimelineEventType, name="timeline_event_type")
HUMAN_INTERACTION_TYPE_ENUM = build_sql_enum(HumanInteractionType, name="human_interaction_type")
HUMAN_INTERACTION_STATUS_ENUM = build_sql_enum(
    HumanInteractionStatus, name="human_interaction_status"
)
HUMAN_INTERACTION_HITL_MODE_ENUM = build_sql_enum(
    HumanInteractionHitlMode, name="human_interaction_hitl_mode"
)
WORKER_RUNTIME_MODE_ENUM = build_sql_enum(WorkerRuntimeMode, name="worker_runtime_mode")
ORCHESTRATION_RUNTIME_ENUM = build_sql_enum(OrchestrationRuntime, name="orchestration_runtime")
PROPOSAL_STATUS_ENUM = build_sql_enum(ProposalStatus, name="proposal_status")
PROPOSAL_TYPE_ENUM = build_sql_enum(ProposalType, name="proposal_type")
MEMORY_PROPOSAL_CATEGORY_ENUM = build_sql_enum(
    MemoryProposalCategory, name="memory_proposal_category"
)
MEMORY_PROPOSAL_STATUS_ENUM = build_sql_enum(MemoryProposalStatus, name="memory_proposal_status")
EXECUTION_PLAN_NODE_STATUS_ENUM = build_sql_enum(
    ExecutionPlanNodeStatus, name="execution_plan_node_status"
)


class EncryptedJSON(TypeDecorator):
    """
    Encrypts/decrypts JSON data at rest using cryptography.fernet.
    Expects CODE_AGENT_ENCRYPTION_KEY environment variable.
    """

    impl = Text
    cache_ok = True
    _cached_fernet: Fernet | None = None
    _last_key: str | None = None
    _lock = threading.Lock()

    def is_active(self) -> bool:
        """Return True if encryption is correctly configured."""
        try:
            return self.fernet is not None
        except RuntimeError:
            return False

    @property
    def fernet(self) -> Fernet | None:
        """Lazily initialize and cache Fernet from environment in a thread-safe manner."""
        key = os.environ.get("CODE_AGENT_ENCRYPTION_KEY")

        # Fast path for already initialized cache (including the "no key" disabled state)
        if key == EncryptedJSON._last_key:
            return EncryptedJSON._cached_fernet

        with EncryptedJSON._lock:
            # Re-read inside the lock to handle rapid env changes atomically
            key = os.environ.get("CODE_AGENT_ENCRYPTION_KEY")
            # Double-check inside the lock
            if key == EncryptedJSON._last_key:
                return EncryptedJSON._cached_fernet

            if not key:
                EncryptedJSON._last_key = None
                EncryptedJSON._cached_fernet = None
                return None

            try:
                new_fernet = Fernet(key.encode())
                EncryptedJSON._cached_fernet = new_fernet
                EncryptedJSON._last_key = key
                return new_fernet
            except Exception as e:
                logger.error("Invalid CODE_AGENT_ENCRYPTION_KEY provided")
                raise RuntimeError("Encryption is configured but the key is invalid.") from e

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Fail fast if a key is already present in the environment but invalid.
        try:
            _ = self.fernet
        except RuntimeError:
            raise

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        json_str = json.dumps(value)
        if self.fernet:
            return self.fernet.encrypt(json_str.encode()).decode()
        return json_str

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if self.fernet:
            # If it looks like a Fernet token, we MUST be able to decrypt it.
            if isinstance(value, str) and value.startswith("gAAAA"):
                try:
                    decrypted = self.fernet.decrypt(value.encode()).decode()
                    return json.loads(decrypted)
                except InvalidToken:
                    logger.critical(
                        "SECURITY CRITICAL: Failed to decrypt secret with active Fernet key. "
                        "This indicates a key mismatch (wrong CODE_AGENT_ENCRYPTION_KEY) "
                        "or data corruption for an already encrypted field."
                    )
                    raise

            # Not a Fernet token — legacy plain JSON (migration compatibility).
            # Return directly; do not fall through to the outer fallback block.
            try:
                return json.loads(value)
            except (ValueError, json.JSONDecodeError):
                logger.warning(
                    "Encryption is active but value is not a Fernet token and not valid JSON; "
                    "returning raw value."
                )
                return value

        # Encryption inactive — parse plain JSON or return raw.
        try:
            return json.loads(value)
        except (ValueError, json.JSONDecodeError, TypeError):
            return value


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A known user who can own sessions."""

    __tablename__ = "users"

    external_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sessions: Mapped[list[Session]] = relationship(back_populates="user")


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
    proposals: Mapped[list[Proposal]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )

    @validates("status")
    def _coerce_status(self, _key: str, value: SessionStatus | str) -> SessionStatus:
        """Normalize assigned session statuses to the canonical enum."""

        return SessionStatus(value)


class Task(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A requested unit of work within a session."""

    @classmethod
    def is_secret_encryption_active(cls) -> bool:
        """Return True if the secrets column is configured for encryption."""
        column_type = cls.__table__.c.secrets.type
        if isinstance(column_type, EncryptedJSON):
            return column_type.is_active()
        return False

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
    task_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secrets: Mapped[dict[str, str]] = mapped_column(EncryptedJSON, nullable=False, default=dict)
    secrets_encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[TaskStatus] = mapped_column(
        TASK_STATUS_ENUM,
        nullable=False,
        default=TaskStatus.PENDING,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queue_lane: Mapped[str] = mapped_column(String(50), nullable=False, default="primary")
    chosen_worker: Mapped[WorkerType | None] = mapped_column(WORKER_TYPE_ENUM, nullable=True)
    chosen_profile: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runtime_mode: Mapped[WorkerRuntimeMode | None] = mapped_column(
        WORKER_RUNTIME_MODE_ENUM, nullable=True
    )
    orchestration_runtime: Mapped[OrchestrationRuntime | None] = mapped_column(
        ORCHESTRATION_RUNTIME_ENUM, nullable=True, index=True
    )
    route_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    trace_context: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    repair_for_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )

    session: Mapped[Session] = relationship(back_populates="tasks")
    worker_runs: Mapped[list[WorkerRun]] = relationship(back_populates="task")
    inbound_deliveries: Mapped[list[InboundDelivery]] = relationship(back_populates="task")
    human_interactions: Mapped[list[HumanInteraction]] = relationship(back_populates="task")
    timeline_events: Mapped[list[TaskTimelineEvent]] = relationship(
        back_populates="task",
        order_by="TaskTimelineEvent.attempt_number.asc(), TaskTimelineEvent.sequence_number.asc()",
    )
    proposals: Mapped[list[Proposal]] = relationship(back_populates="task", passive_deletes=True)
    execution_plan: Mapped[ExecutionPlan | None] = relationship(
        back_populates="task", passive_deletes=True
    )
    temporal_state: Mapped[TemporalTaskState | None] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    temporal_commands: Mapped[list[TemporalCommand]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )
    repair_for_task: Mapped[Task | None] = relationship(
        remote_side="Task.id", back_populates="repairing_tasks"
    )
    repairing_tasks: Mapped[list[Task]] = relationship(back_populates="repair_for_task")

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
        return coerce_worker_type(value)

    @validates("worker_override")
    def _coerce_worker_override(
        self,
        _key: str,
        value: WorkerType | str | None,
    ) -> WorkerType | None:
        """Normalize assigned worker overrides to the canonical enum vocabulary."""
        if value is None:
            return None
        return coerce_worker_type(value)

    @validates("runtime_mode")
    def _coerce_runtime_mode(
        self,
        _key: str,
        value: WorkerRuntimeMode | str | None,
    ) -> WorkerRuntimeMode | None:
        """Normalize assigned runtime modes to the canonical enum."""
        if value is None:
            return None
        return WorkerRuntimeMode(value)

    @validates("orchestration_runtime")
    def _coerce_orchestration_runtime(
        self,
        _key: str,
        value: OrchestrationRuntime | str | None,
    ) -> OrchestrationRuntime | None:
        """Normalize the runtime and prevent changing an already pinned task."""

        normalized = OrchestrationRuntime(value) if value is not None else None
        existing = self.__dict__.get("orchestration_runtime")
        if existing is not None and normalized != existing:
            raise ValueError("Task orchestration_runtime is immutable after creation.")
        return normalized


class TemporalTaskState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Encrypted durable activity handoff state for a Temporal task workflow."""

    __tablename__ = "temporal_task_states"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    state: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON, nullable=False)

    task: Mapped[Task] = relationship(back_populates="temporal_state")


class TemporalCommand(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Transactional commands awaiting idempotent delivery to Temporal."""

    __tablename__ = "temporal_commands"
    __table_args__ = (
        UniqueConstraint("command_key", name="uq_temporal_commands_command_key"),
        UniqueConstraint("task_id", "sequence_number", name="uq_temporal_commands_task_sequence"),
        Index("ix_temporal_commands_task_sequence", "task_id", "sequence_number"),
    )

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    command_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    command_key: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task] = relationship(back_populates="temporal_commands")


class RuntimeCutover(TimestampMixin, Base):
    """Immutable deployment cutover evidence used by operational retirement gates."""

    __tablename__ = "runtime_cutovers"

    cutover_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    cutover_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    release_identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ExecutionCapacityPermit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One durable lease slot for bounded execution-queue concurrency."""

    __tablename__ = "execution_capacity_permits"
    __table_args__ = (
        UniqueConstraint("queue_name", "slot_index", name="uq_execution_capacity_slot"),
    )

    queue_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
    retention_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    status: Mapped[WorkerRunStatus] = mapped_column(WORKER_RUN_STATUS_ENUM, nullable=False)
    worker_profile: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runtime_mode: Mapped[WorkerRuntimeMode | None] = mapped_column(
        WORKER_RUNTIME_MODE_ENUM, nullable=True
    )
    orchestration_runtime: Mapped[OrchestrationRuntime | None] = mapped_column(
        ORCHESTRATION_RUNTIME_ENUM, nullable=True, index=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_permission: Mapped[str | None] = mapped_column(String(64), nullable=True)
    budget_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verifier_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    commands_run: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    files_changed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    files_changed: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    artifact_index: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    runtime_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    delivery_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    task: Mapped[Task] = relationship(back_populates="worker_runs")
    session: Mapped[Session | None] = relationship(back_populates="worker_runs")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="worker_run")

    @validates("worker_type")
    def _coerce_worker_type(self, _key: str, value: WorkerType | str) -> WorkerType:
        """Normalize assigned worker types to the canonical enum."""

        return coerce_worker_type(value)

    @validates("status")
    def _coerce_status(
        self,
        _key: str,
        value: WorkerRunStatus | str,
    ) -> WorkerRunStatus:
        """Normalize assigned worker-run statuses to the canonical enum."""

        return WorkerRunStatus(value)

    @validates("runtime_mode")
    def _coerce_runtime_mode(
        self,
        _key: str,
        value: WorkerRuntimeMode | str | None,
    ) -> WorkerRuntimeMode | None:
        """Normalize assigned runtime modes to the canonical enum."""
        if value is None:
            return None
        return WorkerRuntimeMode(value)

    @validates("orchestration_runtime")
    def _coerce_orchestration_runtime(
        self,
        _key: str,
        value: OrchestrationRuntime | str | None,
    ) -> OrchestrationRuntime | None:
        """Normalize runtime evidence and prevent mutation after creation."""

        normalized = OrchestrationRuntime(value) if value is not None else None
        existing = self.__dict__.get("orchestration_runtime")
        if existing is not None and normalized != existing:
            raise ValueError("WorkerRun orchestration_runtime is immutable after creation.")
        return normalized


class WorkerNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A queue worker process that can claim and execute persisted tasks."""

    __tablename__ = "worker_nodes"
    __table_args__ = (
        CheckConstraint("capacity > 0", name="worker_capacity_positive"),
        CheckConstraint("current_load >= 0", name="worker_load_nonnegative"),
        CheckConstraint("current_load <= capacity", name="worker_load_within_capacity"),
        CheckConstraint("consecutive_failures >= 0", name="worker_failures_nonnegative"),
    )

    worker_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    worker_type: Mapped[WorkerType] = mapped_column(WORKER_TYPE_ENUM, nullable=False, index=True)
    status: Mapped[WorkerNodeStatus] = mapped_column(
        WORKER_NODE_STATUS_ENUM,
        nullable=False,
        default=WorkerNodeStatus.ACTIVE,
        index=True,
    )
    process_identity: Mapped[str | None] = mapped_column(String(255), nullable=True)
    supported_profiles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        index=True,
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_load: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    @validates("worker_type")
    def _coerce_worker_type(self, _key: str, value: WorkerType | str) -> WorkerType:
        """Normalize assigned worker types to the canonical enum."""

        return coerce_worker_type(value)

    @validates("status")
    def _coerce_status(self, _key: str, value: WorkerNodeStatus | str) -> WorkerNodeStatus:
        """Normalize assigned worker-node statuses to the canonical enum."""

        return WorkerNodeStatus(value)


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
    """Structured operator-global personal memory entries."""

    __tablename__ = "memory_personal"
    __table_args__ = (UniqueConstraint("memory_key", name="uq_memory_personal_key"),)

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


class MemoryObservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Raw episodic task/session observations."""

    __tablename__ = "memory_observations"
    __table_args__ = (
        CheckConstraint(
            "admission_status IN ('not_required', 'pending', 'processed', 'invalid', 'failed')",
            name="memory_observation_admission_status",
        ),
    )

    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    worker_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    privacy_stripped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    admission_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="not_required",
        index=True,
    )
    admission_processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    admission_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped[Task | None] = relationship()
    session: Mapped[Session | None] = relationship()


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
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_number",
            "sequence_number",
            name="uq_task_timeline_events_task_attempt_seq",
        ),
        UniqueConstraint("task_id", "event_key", name="uq_task_timeline_events_task_event_key"),
    )

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
    event_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
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


class HumanInteraction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A persisted human checkpoint requiring operator intervention."""

    __tablename__ = "human_interactions"
    __table_args__ = (Index("ix_human_interactions_task_id_status", "task_id", "status"),)

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    hitl_mode: Mapped[HumanInteractionHitlMode] = mapped_column(
        HUMAN_INTERACTION_HITL_MODE_ENUM,
        nullable=False,
        default=HumanInteractionHitlMode.REQUIRE_APPROVAL,
    )
    interaction_type: Mapped[HumanInteractionType] = mapped_column(
        HUMAN_INTERACTION_TYPE_ENUM, nullable=False
    )
    status: Mapped[HumanInteractionStatus] = mapped_column(
        HUMAN_INTERACTION_STATUS_ENUM, nullable=False, default=HumanInteractionStatus.PENDING
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    response_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    task: Mapped[Task] = relationship(back_populates="human_interactions")

    @validates("interaction_type")
    def _coerce_interaction_type(
        self, _key: str, value: HumanInteractionType | str
    ) -> HumanInteractionType:
        """Normalize assigned interaction types to the canonical enum."""
        return HumanInteractionType(value)

    @validates("status")
    def _coerce_status(
        self, _key: str, value: HumanInteractionStatus | str
    ) -> HumanInteractionStatus:
        """Normalize assigned status to the canonical enum."""
        return HumanInteractionStatus(value)

    @validates("hitl_mode")
    def _coerce_hitl_mode(
        self, _key: str, value: HumanInteractionHitlMode | str
    ) -> HumanInteractionHitlMode:
        """Normalize assigned hitl_mode to the canonical enum."""
        return HumanInteractionHitlMode(value)


class Proposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An idea or code proposal emitted by a task for later review."""

    __tablename__ = "proposals"
    __table_args__ = (Index("ix_proposals_created_at", "created_at"),)

    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProposalStatus] = mapped_column(
        PROPOSAL_STATUS_ENUM,
        nullable=False,
        default=ProposalStatus.PENDING_REVIEW,
        index=True,
    )
    proposal_type: Mapped[ProposalType] = mapped_column(
        PROPOSAL_TYPE_ENUM,
        nullable=False,
        default=ProposalType.SCOUT,
        index=True,
    )
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    session: Mapped[Session] = relationship(back_populates="proposals")
    task: Mapped[Task | None] = relationship(back_populates="proposals")

    @validates("status")
    def _coerce_status(self, _key: str, value: ProposalStatus | str) -> ProposalStatus:
        """Normalize assigned status to the canonical enum."""
        return ProposalStatus(value)

    @validates("proposal_type")
    def _coerce_proposal_type(self, _key: str, value: ProposalType | str) -> ProposalType:
        """Normalize assigned proposal_type to the canonical enum."""
        return ProposalType(value)


class MemoryProposal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reviewable candidate memory entry accepted directly into skeptical memory."""

    __tablename__ = "memory_proposals"
    __table_args__ = (
        CheckConstraint(
            "((category = 'project' AND repo_url IS NOT NULL) OR "
            "(category = 'personal' AND repo_url IS NULL))",
            name="category_repo_url",
        ),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="confidence_range",
        ),
        Index("ix_memory_proposals_status_created_at", "status", "created_at"),
        Index("ix_memory_proposals_category_status", "category", "status"),
        Index(
            "uq_idx_proposal_source_observation_id",
            "source_observation_id",
            unique=True,
            postgresql_where=text("source_observation_id IS NOT NULL"),
            sqlite_where=text("source_observation_id IS NOT NULL"),
        ),
    )

    category: Mapped[MemoryProposalCategory] = mapped_column(
        MEMORY_PROPOSAL_CATEGORY_ENUM,
        nullable=False,
        index=True,
    )
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    memory_key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requires_verification: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[MemoryProposalStatus] = mapped_column(
        MEMORY_PROPOSAL_STATUS_ENUM,
        nullable=False,
        default=MemoryProposalStatus.PENDING_REVIEW,
        index=True,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("memory_observations.id", ondelete="SET NULL"),
        nullable=True,
    )
    accepted_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @validates("category")
    def _coerce_category(
        self,
        _key: str,
        value: MemoryProposalCategory | str,
    ) -> MemoryProposalCategory:
        """Normalize assigned memory proposal categories."""
        return MemoryProposalCategory(value)

    @validates("status")
    def _coerce_status(
        self,
        _key: str,
        value: MemoryProposalStatus | str,
    ) -> MemoryProposalStatus:
        """Normalize assigned memory proposal statuses."""
        return MemoryProposalStatus(value)


class MemoryAdmissionDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Inspectable decision made while admitting a candidate memory."""

    __tablename__ = "memory_admission_decisions"
    __table_args__ = (
        CheckConstraint(
            "category IN ('personal', 'project')",
            name="memory_admission_category",
        ),
        CheckConstraint(
            "decision IN ('reject', 'create', 'update', 'merge', 'needs_human_review')",
            name="memory_admission_decision",
        ),
        CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'blocked')",
            name="memory_admission_risk_level",
        ),
        Index("ix_memory_admission_decisions_decision_created_at", "decision", "created_at"),
        Index(
            "uq_idx_decision_source_observation_id",
            "source_observation_id",
            unique=True,
            postgresql_where=text("source_observation_id IS NOT NULL"),
            sqlite_where=text("source_observation_id IS NOT NULL"),
        ),
    )

    category: Mapped[str] = mapped_column(String(8), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decision: Mapped[str] = mapped_column(String(18), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(7), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    durable_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(
        ForeignKey("memory_proposals.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("memory_observations.id", ondelete="SET NULL"),
        nullable=True,
    )


class ExecutionPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An observable spine of planned work for a complex task."""

    __tablename__ = "execution_plans"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    task: Mapped[Task] = relationship(back_populates="execution_plan")
    nodes: Mapped[list[ExecutionPlanNode]] = relationship(
        back_populates="execution_plan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExecutionPlanNode.sequence_number.asc()",
    )


class ExecutionPlanNode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single node within an execution plan."""

    __tablename__ = "execution_plan_nodes"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "node_id",
            name="uq_execution_plan_nodes_plan_id_node_id",
        ),
    )

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("execution_plans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    depends_on: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    task_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    node_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    aggregation_role: Mapped[str] = mapped_column(String(50), nullable=False, default="mutation")
    execution_mode: Mapped[str] = mapped_column(String(50), nullable=False, default="mutable")
    parallel_safe: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[ExecutionPlanNodeStatus] = mapped_column(
        EXECUTION_PLAN_NODE_STATUS_ENUM, nullable=False, default=ExecutionPlanNodeStatus.PENDING
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_worker_profile: Mapped[str | None] = mapped_column(String(255), nullable=True)
    budget: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    validation_commands: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    artifacts: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    blocker_interaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("human_interactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("worker_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    verification_outcome: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    changed_files: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    output_artifacts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_logical_activity_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    terminal_result_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terminal_result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    terminal_result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    execution_plan: Mapped[ExecutionPlan] = relationship(back_populates="nodes")
    attempts: Mapped[list[ExecutionPlanNodeAttempt]] = relationship(
        back_populates="plan_node",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: ExecutionPlanNodeAttempt.attempt_number.asc(),
    )
    blocker_interaction: Mapped[HumanInteraction | None] = relationship()

    @validates("status")
    def _coerce_status(
        self, _key: str, value: ExecutionPlanNodeStatus | str
    ) -> ExecutionPlanNodeStatus:
        """Normalize assigned plan node statuses to the canonical enum."""

        return ExecutionPlanNodeStatus(value)


class ExecutionPlanNodeAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Durable, append-only execution evidence for one DAG node attempt."""

    __tablename__ = "execution_plan_node_attempts"
    __table_args__ = (
        UniqueConstraint("plan_node_id", "attempt_number", name="uq_plan_node_attempt_number"),
        UniqueConstraint(
            "plan_node_id", "logical_activity_key", name="uq_plan_node_attempt_activity_key"
        ),
    )

    plan_node_id: Mapped[str] = mapped_column(
        ForeignKey("execution_plan_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    task_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    worker_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    worker_profile: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runtime_mode: Mapped[str | None] = mapped_column(String(50), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="started")
    failure_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    effective_input_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    effective_input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_activity_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    plan_node: Mapped[ExecutionPlanNode] = relationship(back_populates="attempts")
