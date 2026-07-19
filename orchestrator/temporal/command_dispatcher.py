"""Worker-owned reconciliation loop for transactional Temporal commands."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Any

import temporalio.exceptions

from db.base import utc_now
from db.models import TemporalCommand
from repositories import TemporalCommandRepository, session_scope

logger = logging.getLogger(__name__)


class TemporalCommandDispatcher:
    """Deliver pending commands; undelivered rows survive worker/API restarts."""

    def __init__(
        self,
        *,
        client: Any,
        session_factory: Any,
        batch_size: int = 20,
        claim_lease_seconds: int = 30,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.batch_size = batch_size
        self.claim_lease_seconds = claim_lease_seconds

    async def run_forever(self) -> None:
        while True:
            await self.dispatch_pending()
            await asyncio.sleep(1)

    async def dispatch_pending(self) -> None:
        with session_scope(self.session_factory) as session:
            commands = TemporalCommandRepository(session).claim_pending(
                limit=self.batch_size, lease_seconds=self.claim_lease_seconds
            )
            command_refs = [(command.id, command.claim_token) for command in commands]
        for command_id, claim_token in command_refs:
            if claim_token is not None:
                await self._dispatch_one(command_id, claim_token)

    async def _dispatch_one(self, command_id: str, claim_token: str) -> None:
        with session_scope(self.session_factory) as session:
            command = session.get(TemporalCommand, command_id)
            if command is None or command.claim_token != claim_token:
                return
            command_type = command.command_type
            command_key = command.command_key
            payload = dict(command.payload)
            task_id = command.task_id
        try:
            await self._deliver(
                task_id=task_id,
                command_type=command_type,
                command_key=command_key,
                payload=payload,
            )
        except Exception as exc:
            self._record_failure(command_id, claim_token, exc)
        else:
            with session_scope(self.session_factory) as session:
                acknowledged = TemporalCommandRepository(session).mark_delivered(
                    command_id=command_id, claim_token=claim_token
                )
            if not acknowledged:
                logger.warning(
                    "Temporal command acknowledgement lost claim",
                    extra={"command_id": command_id},
                )

    def _record_failure(self, command_id: str, claim_token: str, error: Exception) -> None:
        retry_at = None if self._is_non_retryable(error) else self._retry_at(command_id)
        with session_scope(self.session_factory) as session:
            repo = TemporalCommandRepository(session)
            persisted = repo.mark_failed(
                command_id=command_id,
                claim_token=claim_token,
                error=error,
                retry_at=retry_at,
            )
        if persisted:
            logger.warning(
                "Temporal command delivery failed",
                extra={
                    "command_id": command_id,
                    "retry_at": retry_at.isoformat() if retry_at else None,
                },
            )

    def _retry_at(self, command_id: str) -> datetime:
        # Bounded exponential backoff plus small jitter prevents hot-loop retries.
        with session_scope(self.session_factory) as session:
            command = session.get(TemporalCommand, command_id)
            attempts = command.attempts if command is not None else 0
        seconds = min(300, 2 ** min(attempts, 8))
        return utc_now() + timedelta(seconds=seconds + random.random())

    @staticmethod
    def _is_non_retryable(error: Exception) -> bool:
        return isinstance(error, ValueError)

    async def _deliver(
        self,
        *,
        task_id: str,
        command_type: str,
        command_key: str,
        payload: dict[str, Any],
    ) -> None:
        workflow_id = f"task-{task_id}"
        if command_type == "start":
            try:
                await self.client.start_workflow(
                    "TaskExecutionWorkflow",
                    task_id,
                    id=workflow_id,
                    task_queue="task-execution-queue",
                )
            except temporalio.exceptions.WorkflowAlreadyStartedError:
                return
            return
        handle = self.client.get_workflow_handle(workflow_id)
        if command_type == "signal":
            await handle.signal(
                payload["signal_name"],
                {"command_key": command_key, "value": payload.get("signal_arg")},
            )
            return
        if command_type == "cancel":
            await handle.cancel()
            return
        raise ValueError(f"Unknown Temporal command type: {command_type}")
