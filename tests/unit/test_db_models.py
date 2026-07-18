"""Unit tests for the initial ORM metadata."""

from __future__ import annotations

import pytest
from sqlalchemy import JSON, DateTime, create_engine
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.exc import IntegrityError

import db.models  # noqa: F401
from db.base import Base
from db.enums import (
    ArtifactType,
    ExecutionPlanNodeStatus,
    HumanInteractionHitlMode,
    HumanInteractionStatus,
    HumanInteractionType,
    MemoryProposalCategory,
    MemoryProposalStatus,
    ProposalStatus,
    ProposalType,
    SessionStatus,
    TaskStatus,
    TimelineEventType,
    WorkerNodeStatus,
    WorkerRunStatus,
    WorkerType,
)

EXPECTED_TABLES = {
    "artifacts",
    "execution_plans",
    "execution_plan_nodes",
    "execution_plan_node_attempts",
    "execution_capacity_permits",
    "human_interactions",
    "inbound_deliveries",
    "memory_admission_decisions",
    "memory_observations",
    "memory_personal",
    "memory_proposals",
    "memory_project",
    "proposals",
    "sessions",
    "session_states",
    "tasks",
    "temporal_task_states",
    "task_timeline_events",
    "users",
    "worker_nodes",
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
        ("worker_nodes", "worker_type"): WorkerType,
        ("worker_nodes", "status"): WorkerNodeStatus,
        ("worker_runs", "worker_type"): WorkerType,
        ("worker_runs", "status"): WorkerRunStatus,
        ("artifacts", "artifact_type"): ArtifactType,
        ("human_interactions", "interaction_type"): HumanInteractionType,
        ("human_interactions", "status"): HumanInteractionStatus,
        ("human_interactions", "hitl_mode"): HumanInteractionHitlMode,
        ("memory_proposals", "category"): MemoryProposalCategory,
        ("memory_proposals", "status"): MemoryProposalStatus,
        ("proposals", "status"): ProposalStatus,
        ("proposals", "proposal_type"): ProposalType,
        ("task_timeline_events", "event_type"): TimelineEventType,
        ("execution_plan_nodes", "status"): ExecutionPlanNodeStatus,
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


def test_model_metadata_defines_trace_context_column_type() -> None:
    """Distributed tracing needs a serializable JSON contract on tasks."""
    column_type = Base.metadata.tables["tasks"].c["trace_context"].type
    assert isinstance(column_type, JSON)


def test_model_metadata_defines_runtime_manifest_column_type() -> None:
    """Runtime operating contract metadata needs a JSON column on worker runs."""
    column_type = Base.metadata.tables["worker_runs"].c["runtime_manifest"].type
    assert isinstance(column_type, JSON)


def test_execution_capacity_permits_store_a_fenced_acquisition_token() -> None:
    """Permit release and renewal require more than a reusable logical owner."""
    assert "lease_token" in Base.metadata.tables["execution_capacity_permits"].c


def test_model_metadata_enforces_memory_observation_constraints() -> None:
    """Metadata-created DBs should match migration constraints for observations."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    observations = Base.metadata.tables["memory_observations"]
    proposals = Base.metadata.tables["memory_proposals"]
    decisions = Base.metadata.tables["memory_admission_decisions"]

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                observations.insert().values(
                    id="obs-invalid-status",
                    source="worker",
                    event_type="worker_completed",
                    summary="summary",
                    content="content",
                    metadata_payload={},
                    privacy_stripped=False,
                    admission_status="surprising",
                )
            )

    with engine.begin() as connection:
        connection.execute(
            proposals.insert(),
            [
                _proposal_values("proposal-null-1", "key-null-1", None),
                _proposal_values("proposal-null-2", "key-null-2", None),
                _proposal_values("proposal-source-1", "key-source-1", "obs-1"),
            ],
        )
        connection.execute(
            decisions.insert(),
            [
                _decision_values("decision-null-1", "key-null-1", None),
                _decision_values("decision-null-2", "key-null-2", None),
                _decision_values("decision-source-1", "key-source-1", "obs-1"),
            ],
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                proposals.insert().values(
                    _proposal_values("proposal-source-2", "key-source-2", "obs-1")
                )
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                decisions.insert().values(
                    _decision_values("decision-source-2", "key-source-2", "obs-1")
                )
            )


def _proposal_values(
    proposal_id: str,
    memory_key: str,
    source_observation_id: str | None,
) -> dict[str, object]:
    return {
        "id": proposal_id,
        "category": MemoryProposalCategory.PERSONAL.value,
        "repo_url": None,
        "memory_key": memory_key,
        "value": {},
        "confidence": 1.0,
        "requires_verification": True,
        "status": MemoryProposalStatus.PENDING_REVIEW.value,
        "source_observation_id": source_observation_id,
    }


def _decision_values(
    decision_id: str,
    memory_key: str,
    source_observation_id: str | None,
) -> dict[str, object]:
    return {
        "id": decision_id,
        "category": "personal",
        "memory_key": memory_key,
        "candidate_payload": {},
        "decision": "create",
        "risk_level": "low",
        "reason": "Looks useful.",
        "source_observation_id": source_observation_id,
    }
