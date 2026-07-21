"""Milestone readiness lifecycle and bounded task-policy helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from db.base import utc_now
from db.enums import MilestoneAutonomyMode, MilestoneReadinessStatus, MilestoneStatus, TaskStatus
from db.models import Milestone, MilestoneReadinessAssessment, Task
from orchestrator.execution_types import (
    MilestoneReadinessSnapshot,
    MilestoneSnapshot,
    TaskSubmission,
    TaskSubmissionValidationError,
)
from repositories import MilestoneRepository, session_scope

_SEED_MILESTONES = (
    ("M25.3", "Temporal observation evidence ledger", 253, MilestoneStatus.ACTIVE),
    ("M26", "Temporal rollout completion", 260, MilestoneStatus.PLANNED),
    ("M27", "Milestone readiness and autonomy graduation", 270, MilestoneStatus.PLANNED),
)
_HIGH_RISK_ACTIONS = (
    "auto_merge",
    "deploy",
    "security_auth_billing_sandbox_policy_change",
    "new_secret_access",
    "destructive_operation",
)
_MODE_RANK = {
    MilestoneAutonomyMode.HUMAN_LED: 0,
    MilestoneAutonomyMode.AGENT_LED_APPROVAL_GATED: 1,
    MilestoneAutonomyMode.AUTONOMOUS_DELIVERY: 2,
}


def _seed_milestones(session: Any) -> None:
    existing = {row.key: row for row in session.scalars(select(Milestone)).all()}
    for key, title, sequence, status in _SEED_MILESTONES:
        if key not in existing:
            row = Milestone(key=key, title=title, sequence=sequence, status=status)
            session.add(row)
            existing[key] = row
    session.flush()
    for current, successor in zip(_SEED_MILESTONES, _SEED_MILESTONES[1:], strict=False):
        existing[current[0]].successor_id = existing[successor[0]].id
    session.flush()


def _milestone_snapshot(row: Milestone) -> MilestoneSnapshot:
    return MilestoneSnapshot(
        milestone_id=row.id,
        key=row.key,
        title=row.title,
        sequence=row.sequence,
        status=row.status,
        successor_id=row.successor_id,
        active_autonomy_mode=row.active_autonomy_mode,
        completed_at=row.completed_at,
    )


def _assessment_snapshot(row: MilestoneReadinessAssessment) -> MilestoneReadinessSnapshot:
    return MilestoneReadinessSnapshot(
        assessment_id=row.id,
        completed_milestone_id=row.completed_milestone_id,
        next_milestone_id=row.next_milestone_id,
        status=row.status,
        evidence_snapshot=row.evidence_snapshot,
        rubric=row.rubric,
        reviewer_narrative=row.reviewer_narrative,
        recommended_mode=row.recommended_mode,
        approved_mode=row.approved_mode,
        decision_reason=row.decision_reason,
        reviewed_at=row.reviewed_at,
        decided_at=row.decided_at,
    )


def _review_assessment(session: Any, assessment: MilestoneReadinessAssessment) -> None:
    """Produce deterministic, read-only evidence and an advisory recommendation."""
    task_counts = dict(
        session.execute(
            select(Task.status, func.count(Task.id))
            .where(Task.milestone_id == assessment.completed_milestone_id)
            .group_by(Task.status)
        ).all()
    )
    total = sum(task_counts.values())
    terminal_failures = task_counts.get(TaskStatus.FAILED, 0)
    completed = task_counts.get(TaskStatus.COMPLETED, 0)
    success_rate = completed / total if total else 0.0
    evidence = {
        "captured_at": utc_now().isoformat(),
        "task_counts_by_status": {str(key.value): value for key, value in task_counts.items()},
        "total_tasks": total,
        "completed_tasks": completed,
        "failed_tasks": terminal_failures,
        "prior_assessment_outcomes": [],
    }
    rubric = {
        "delivery_reliability": {"confidence": round(success_rate, 2), "evidence": "task outcomes"},
        "operator_independence": {
            "confidence": 0.0,
            "evidence": "intervention telemetry unavailable",
        },
        "safety_and_recovery": {
            "confidence": 0.0,
            "evidence": "incident and rollback telemetry unavailable",
        },
        "successor_readiness": {"confidence": 0.0, "evidence": "operator review required"},
        "blocked_capabilities": list(_HIGH_RISK_ACTIONS),
        "required_checkpoints": ["operator approval", "existing privileged-action approval"],
    }
    recommendation = MilestoneAutonomyMode.HUMAN_LED
    if total >= 5 and success_rate >= 0.9 and terminal_failures == 0:
        recommendation = MilestoneAutonomyMode.AGENT_LED_APPROVAL_GATED
    assessment.evidence_snapshot = evidence
    assessment.rubric = rubric
    assessment.reviewer_narrative = (
        "Read-only deterministic review completed from persisted task outcomes. "
        "Recommendation is advisory and does not change successor policy."
    )
    assessment.recommended_mode = recommendation
    assessment.status = MilestoneReadinessStatus.PENDING_APPROVAL
    assessment.reviewed_at = utc_now()


def list_milestones(self: Any) -> list[MilestoneSnapshot]:
    with session_scope(self.session_factory) as session:
        _seed_milestones(session)
        return [_milestone_snapshot(row) for row in MilestoneRepository(session).list()]


def get_milestone(self: Any, milestone_id: str) -> MilestoneSnapshot | None:
    with session_scope(self.session_factory) as session:
        _seed_milestones(session)
        row = session.get(Milestone, milestone_id)
        return None if row is None else _milestone_snapshot(row)


def list_milestone_readiness_assessments(self: Any) -> list[MilestoneReadinessSnapshot]:
    with session_scope(self.session_factory) as session:
        _seed_milestones(session)
        rows = session.scalars(
            select(MilestoneReadinessAssessment).order_by(
                MilestoneReadinessAssessment.created_at.desc()
            )
        ).all()
        return [_assessment_snapshot(row) for row in rows]


def complete_milestone(self: Any, milestone_id: str) -> MilestoneReadinessSnapshot:
    with session_scope(self.session_factory) as session:
        _seed_milestones(session)
        milestone = session.get(Milestone, milestone_id)
        if milestone is None:
            raise ValueError(f"Milestone {milestone_id} not found")
        prior = session.scalars(
            select(MilestoneReadinessAssessment)
            .where(MilestoneReadinessAssessment.completed_milestone_id == milestone_id)
            .order_by(MilestoneReadinessAssessment.generation.desc())
        ).first()
        if prior is not None:
            if milestone.status is MilestoneStatus.COMPLETED:
                return _assessment_snapshot(prior)
            prior.status = MilestoneReadinessStatus.SUPERSEDED
            generation = prior.generation + 1
        else:
            generation = 1
        milestone.status = MilestoneStatus.COMPLETED
        milestone.completed_at = utc_now()
        assessment = MilestoneReadinessAssessment(
            completed_milestone_id=milestone.id,
            next_milestone_id=milestone.successor_id,
            generation=generation,
            status=MilestoneReadinessStatus.QUEUED,
            evidence_snapshot={},
            rubric={},
        )
        session.add(assessment)
        session.flush()
        _review_assessment(session, assessment)
        session.flush()
        return _assessment_snapshot(assessment)


def reopen_milestone(self: Any, milestone_id: str) -> MilestoneSnapshot:
    """Reopen a milestone and supersede its latest review evidence."""
    with session_scope(self.session_factory) as session:
        _seed_milestones(session)
        milestone = session.get(Milestone, milestone_id)
        if milestone is None:
            raise ValueError(f"Milestone {milestone_id} not found")
        milestone.status = MilestoneStatus.ACTIVE
        milestone.completed_at = None
        latest = session.scalars(
            select(MilestoneReadinessAssessment)
            .where(MilestoneReadinessAssessment.completed_milestone_id == milestone_id)
            .order_by(MilestoneReadinessAssessment.generation.desc())
        ).first()
        if latest is not None and latest.status is not MilestoneReadinessStatus.SUPERSEDED:
            latest.status = MilestoneReadinessStatus.SUPERSEDED
        session.flush()
        return _milestone_snapshot(milestone)


def decide_milestone_readiness(
    self: Any,
    assessment_id: str,
    *,
    approved: bool,
    mode: MilestoneAutonomyMode | None,
    reason: str | None,
) -> MilestoneReadinessSnapshot:
    with session_scope(self.session_factory) as session:
        assessment = session.get(MilestoneReadinessAssessment, assessment_id)
        if assessment is None:
            raise ValueError(f"Assessment {assessment_id} not found")
        if assessment.status is not MilestoneReadinessStatus.PENDING_APPROVAL:
            raise ValueError("Assessment is not pending operator approval")
        if approved:
            if mode is None:
                mode = assessment.recommended_mode
            recommended_mode = assessment.recommended_mode
            if (
                mode is None
                or recommended_mode is None
                or _MODE_RANK[mode] > _MODE_RANK[recommended_mode]
            ):
                raise ValueError("Operators may select the recommendation or a stricter mode")
        assessment.status = (
            MilestoneReadinessStatus.APPROVED if approved else MilestoneReadinessStatus.REJECTED
        )
        assessment.approved_mode = mode if approved else None
        assessment.decision_reason = reason
        assessment.decided_at = utc_now()
        if approved and assessment.next_milestone_id and mode is not None:
            successor = session.get(Milestone, assessment.next_milestone_id)
            if successor is None:
                raise ValueError("Assessment successor milestone was not found")
            successor.active_autonomy_mode = mode
        session.flush()
        return _assessment_snapshot(assessment)


def _apply_milestone_policy(self: Any, submission: TaskSubmission) -> TaskSubmission:
    if submission.milestone_id is None:
        return submission
    with session_scope(self.session_factory) as session:
        _seed_milestones(session)
        milestone = session.get(Milestone, submission.milestone_id)
        if milestone is None:
            raise TaskSubmissionValidationError(
                f"Milestone '{submission.milestone_id}' was not found"
            )
        constraints = dict(submission.constraints)
        constraints["milestone_policy"] = {
            "milestone_id": milestone.id,
            "milestone_key": milestone.key,
            "mode": milestone.active_autonomy_mode.value,
            "requires_explicit_approval_for": list(_HIGH_RISK_ACTIONS),
        }
        return submission.model_copy(update={"constraints": constraints})
