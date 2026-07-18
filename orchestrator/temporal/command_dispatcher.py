"""Worker-owned reconciliation loop for transactional Temporal commands."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import temporalio.exceptions

from db.models import TemporalCommand
from repositories import TemporalCommandRepository, session_scope

logger = logging.getLogger(__name__)


class TemporalCommandDispatcher:
    """Deliver pending commands; undelivered rows survive worker/API restarts."""

    def __init__(self, *, client: Any, session_factory: Any) -> None:
        self.client = client
        self.session_factory = session_factory

    async def run_forever(self) -> None:
        while True:
            await self.dispatch_pending()
            await asyncio.sleep(1)

    async def dispatch_pending(self) -> None:
        with session_scope(self.session_factory) as session:
            commands = TemporalCommandRepository(session).pending()
            command_ids = [command.id for command in commands]
        for command_id in command_ids:
            await self._dispatch_one(command_id)

    async def _dispatch_one(self, command_id: str) -> None:
        with session_scope(self.session_factory) as session:
            repo = TemporalCommandRepository(session)
            command = session.get(TemporalCommand, command_id)
            if command is None or command.delivered_at is not None:
                return
            try:
                await self._deliver(command)
            except Exception as exc:
                repo.mark_failed(command, exc)
                logger.warning(
                    "Temporal command delivery failed",
                    extra={"command_id": command.id, "type": command.command_type},
                )
            else:
                repo.mark_delivered(command)

    async def _deliver(self, command: Any) -> None:
        workflow_id = f"task-{command.task_id}"
        if command.command_type == "start":
            try:
                await self.client.start_workflow(
                    "TaskExecutionWorkflow",
                    command.task_id,
                    id=workflow_id,
                    task_queue="task-execution-queue",
                )
            except temporalio.exceptions.WorkflowAlreadyStartedError:
                return
            return
        handle = self.client.get_workflow_handle(workflow_id)
        if command.command_type == "signal":
            await handle.signal(command.payload["signal_name"], command.payload.get("signal_arg"))
            return
        if command.command_type == "cancel":
            await handle.cancel()
            return
        raise ValueError(f"Unknown Temporal command type: {command.command_type}")
