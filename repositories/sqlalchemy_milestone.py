"""Persistence for milestone readiness governance."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.base import utc_now
from db.enums import MilestoneAutonomyMode, MilestoneReadinessStatus, MilestoneStatus
from db.models import Milestone, MilestoneReadinessAssessment


class MilestoneRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> list[Milestone]:
        return list(self.session.scalars(select(Milestone).order_by(Milestone.sequence)))

    def get(self, milestone_id: str) -> Milestone:
        row = self.session.get(Milestone, milestone_id)
        if row is None:
            raise ValueError(f"Milestone {milestone_id} not found")
        return row

    def complete(self, milestone_id: str) -> MilestoneReadinessAssessment:
        milestone = self.get(milestone_id)
        milestone.status = MilestoneStatus.COMPLETED
        milestone.completed_at = utc_now()
        assessment = self.session.scalar(
            select(MilestoneReadinessAssessment).where(
                MilestoneReadinessAssessment.completed_milestone_id == milestone_id
            )
        )
        if assessment is None:
            assessment = MilestoneReadinessAssessment(
                completed_milestone_id=milestone_id,
                next_milestone_id=milestone.successor_id,
                status=MilestoneReadinessStatus.QUEUED,
                evidence_snapshot={},
                rubric={},
            )
            self.session.add(assessment)
        self.session.flush()
        return assessment

    def decide(
        self,
        assessment_id: str,
        mode: MilestoneAutonomyMode | None,
        approved: bool,
        reason: str | None,
    ) -> MilestoneReadinessAssessment:
        assessment = self.session.get(MilestoneReadinessAssessment, assessment_id)
        if assessment is None:
            raise ValueError(f"Assessment {assessment_id} not found")
        if assessment.status not in {
            MilestoneReadinessStatus.PENDING_APPROVAL,
            MilestoneReadinessStatus.APPROVED,
            MilestoneReadinessStatus.REJECTED,
        }:
            raise ValueError("Assessment is not ready for a decision")
        assessment.status = (
            MilestoneReadinessStatus.APPROVED if approved else MilestoneReadinessStatus.REJECTED
        )
        assessment.approved_mode = mode if approved else None
        assessment.decision_reason = reason
        assessment.decided_at = utc_now()
        if approved and assessment.next_milestone_id and mode:
            self.get(assessment.next_milestone_id).active_autonomy_mode = mode
        self.session.flush()
        return assessment
