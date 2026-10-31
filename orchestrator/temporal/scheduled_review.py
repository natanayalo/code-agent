"""Finite Temporal workflow and activity for a scheduled dependency review."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from apps.api.config import SystemConfig
from orchestrator.execution_submission_service import ScheduledSessionBusyError
from orchestrator.execution_types import DeliveryKey
from orchestrator.schedule_spec import ScheduleSpec
from orchestrator.scheduled_health import collect_health_snapshot
from orchestrator.scheduled_submission import build_scheduled_submission

logger = logging.getLogger(__name__)


@workflow.defn
class ScheduledReviewWorkflow:
    """Submit one occurrence, then finish; task execution has its own workflow."""

    @workflow.run
    async def run(self, schedule_id: str, spec: dict[str, Any]) -> dict[str, str | None]:
        return await workflow.execute_activity(
            "submit_scheduled_review",
            args=[schedule_id, workflow.info().workflow_id, spec],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )


class ScheduledReviewActivities:
    """Resolve current allowlist configuration and submit through the task service."""

    def __init__(self, service: Any, config: SystemConfig) -> None:
        self.service = service
        self.config = config

    @activity.defn(name="submit_scheduled_review")
    async def submit(
        self, schedule_id: str, occurrence_id: str, raw_spec: dict[str, Any]
    ) -> dict[str, str | None]:
        try:
            spec = ScheduleSpec.model_validate(raw_spec)
            repo_url = self.config.resolve_repo_key(spec.repo_key)
            if not repo_url:
                raise ApplicationError("Repository is no longer allowlisted", non_retryable=True)
            health_snapshot = (
                await asyncio.to_thread(collect_health_snapshot, self.service)
                if spec.purpose == "health_summary"
                else None
            )
            submission = build_scheduled_submission(
                schedule_id=schedule_id,
                spec=spec,
                repo_url=repo_url,
                occurrence_id=occurrence_id,
                health_snapshot=health_snapshot,
            )
            identity = f"{schedule_id}\0{occurrence_id}".encode()
            key = DeliveryKey(channel="schedule", delivery_id=hashlib.sha256(identity).hexdigest())
            try:
                outcome = await asyncio.to_thread(
                    self.service.create_task_outcome,
                    submission,
                    delivery_key=key,
                    skip_if_session_busy=True,
                )
                result = {
                    "outcome": "duplicate" if outcome.duplicate else "submitted",
                    "task_id": outcome.task_snapshot.task_id,
                }
            except ScheduledSessionBusyError:
                result = {"outcome": "skipped_busy", "task_id": None}
            logger.info(
                "Scheduled review submission",
                extra={"schedule_id": schedule_id, "occurrence_id": occurrence_id, **result},
            )
            return result
        except Exception:
            logger.exception(
                "Scheduled review submission failed",
                extra={"schedule_id": schedule_id, "occurrence_id": occurrence_id},
            )
            raise
