"""Fixed task construction for scheduled dependency reviews."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.enums import TaskStatus
from db.models import Session as ConversationSession
from db.models import Task
from orchestrator.execution_types import DeliveryKey, SubmissionSession, TaskSubmission
from orchestrator.schedule_spec import ScheduleSpec
from repositories import InboundDeliveryRepository

DEPENDENCY_REVIEW_TASK = (
    "Review this repository's dependencies without modifying the repository. "
    "Identify current and outdated dependencies, provide supporting evidence, "
    "discuss upgrade considerations, and explicitly describe any limitations. "
    "Return a summary only."
)
HEALTH_SUMMARY_TASK = (
    "Summarize the following service health observations without modifying the repository. "
    "Distinguish healthy, degraded, and unknown or unavailable observations, include observation "
    "times and limitations, and do not infer unsupported diagnoses. Return a summary only.\n"
)


def lock_and_check_occurrence(
    session: Session,
    session_id: str,
    delivery_key: DeliveryKey | None,
    now: datetime,
) -> str | None:
    """Serialize submissions; return a task ID, empty string for skip, or None for new."""
    session.execute(
        update(ConversationSession)
        .where(ConversationSession.id == session_id)
        .values(last_seen_at=now)
    )
    if delivery_key is None:
        raise ValueError("Scheduled submissions require a delivery key.")
    deliveries = InboundDeliveryRepository(session)
    existing = deliveries.get_by_channel_delivery(
        channel=delivery_key.channel, delivery_id=delivery_key.delivery_id
    )
    if existing is not None:
        return existing.task_id or ""
    outstanding = session.scalar(
        select(Task.id)
        .where(
            Task.session_id == session_id,
            Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]),
        )
        .limit(1)
    )
    if outstanding is not None:
        deliveries.create(channel=delivery_key.channel, delivery_id=delivery_key.delivery_id)
        return ""
    return None


def build_scheduled_submission(
    *,
    schedule_id: str,
    spec: ScheduleSpec,
    repo_url: str,
    occurrence_id: str | None = None,
    health_snapshot: dict[str, Any] | None = None,
) -> TaskSubmission:
    """Build a read-only request from an already revalidated repository URL."""
    if spec.purpose == "health_summary":
        if health_snapshot is None:
            raise ValueError("Health summaries require an observation snapshot.")
        task_text = HEALTH_SUMMARY_TASK + json.dumps(health_snapshot, sort_keys=True)
    else:
        task_text = DEPENDENCY_REVIEW_TASK
    constraints: dict[str, Any] = {
        "read_only": True,
        "delivery_mode": "summary",
        "schedule_id": schedule_id,
        "occurrence_id": occurrence_id,
    }
    if spec.purpose == "health_summary":
        constraints["acceptance_criteria"] = [
            "Report observed healthy, degraded, and unavailable or unknown service signals, "
            "observation times, and limitations without unsupported diagnoses."
        ]
    return TaskSubmission(
        task_text=task_text,
        repo_url=repo_url,
        branch=spec.branch,
        constraints=constraints,
        session=SubmissionSession(
            channel="schedule",
            external_user_id="system:scheduler",
            external_thread_id=schedule_id,
        ),
    )
