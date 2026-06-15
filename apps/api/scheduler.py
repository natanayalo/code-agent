"""Background scheduler for proactive task generation."""

import asyncio
import logging
from datetime import UTC, datetime

from apps.api.config import SystemConfig
from orchestrator.execution import TaskExecutionService
from orchestrator.execution_types import DeliveryKey, SubmissionSession, TaskSubmission

logger = logging.getLogger(__name__)


class ScoutScheduler:
    """Spawns scout tasks based on configured intervals or system idleness."""

    def __init__(self, task_service: TaskExecutionService, config: SystemConfig) -> None:
        self.task_service = task_service
        self.config = config
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._last_busy_time = datetime.now(UTC)

    def start(self) -> None:
        """Start the background scheduler loop."""
        if self._running:
            return
        if not self.config.scout_scheduler_enabled:
            logger.info("ScoutScheduler is disabled via config.")
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("ScoutScheduler started.")

    async def stop(self) -> None:
        """Stop the background scheduler loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("ScoutScheduler stopped.")

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self.tick, datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in ScoutScheduler tick: {e}", exc_info=True)

            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    def tick(self, now: datetime) -> None:
        """Evaluate triggers and synchronously submit tasks if needed."""
        if not self.config.scout_scheduler_enabled:
            return
        if not self.config.scout_repo_url:
            logger.warning("Scout scheduler enabled but CODE_AGENT_SCOUT_REPO_URL is not set.")
            return

        is_busy = self.task_service.is_execution_busy()
        if is_busy:
            self._last_busy_time = now
            return

        idle_duration = (now - self._last_busy_time).total_seconds()

        interval_seconds = self.config.scout_schedule_interval_minutes * 60
        if interval_seconds > 0:
            schedule_period = int(now.timestamp() // interval_seconds)
            schedule_delivery_key = DeliveryKey(
                channel="scheduler",
                delivery_id=f"scout_schedule_{schedule_period}",
            )
            if self._submit_scout(schedule_delivery_key, trigger_source="schedule"):
                return

        idle_trigger_seconds = self.config.scout_idle_trigger_minutes * 60
        if idle_trigger_seconds > 0 and idle_duration >= idle_trigger_seconds:
            idle_delivery_key = DeliveryKey(
                channel="scheduler",
                delivery_id=f"scout_idle_{now.strftime('%Y%m%d%H')}",
            )
            self._submit_scout(idle_delivery_key, trigger_source="idle")

    def _submit_scout(self, delivery_key: DeliveryKey, trigger_source: str) -> bool:
        """Submit a scout task with the given delivery key to prevent duplicates.

        Returns:
            bool: True if a new task was created, False if deduped or failed.
        """
        submission = TaskSubmission(
            task_text=self.config.scout_task_text,
            repo_url=self.config.scout_repo_url,
            branch=self.config.scout_branch,
            priority=0,
            constraints={
                "task_type": "scout",
                "trigger_source": trigger_source,
            },
            session=SubmissionSession(
                channel="scheduler",
                external_user_id="system:scout-scheduler",
                external_thread_id="scout-scheduler",
                display_name="Scout Scheduler",
            ),
        )
        try:
            outcome = self.task_service.create_task_outcome(submission, delivery_key=delivery_key)
            if not outcome.duplicate:
                logger.info(
                    "Spawned scout task via %s trigger: %s",
                    trigger_source,
                    outcome.task_snapshot.task_id,
                )
                return True
        except Exception as e:
            logger.error("Failed to spawn scout task via %s trigger: %s", trigger_source, e)
        return False
