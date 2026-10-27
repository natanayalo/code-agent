"""Authenticated management of weekly dependency-review schedules."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleState,
    WorkflowExecutionStatus,
)
from temporalio.service import RPCError, RPCStatusCode

from apps.api.config import SystemConfig
from apps.api.dependencies import (
    get_system_config,
    get_task_service,
    require_any_valid_auth,
)
from orchestrator.execution import TaskExecutionService
from orchestrator.schedule_spec import ScheduleSpec
from orchestrator.temporal.queues import DEFAULT_TEMPORAL_TASK_QUEUE
from repositories import SessionRepository, session_scope

router = APIRouter(
    prefix="/schedules", tags=["schedules"], dependencies=[Depends(require_any_valid_auth)]
)
_PREFIX = "dependency-review:"


async def get_schedule_client() -> Client:
    try:
        return await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc


def _identity(schedule_id: str) -> str:
    if (
        not schedule_id
        or len(schedule_id) > 200
        or not all(char.isascii() and (char.isalnum() or char in "-_.") for char in schedule_id)
    ):
        raise HTTPException(status_code=422, detail="Invalid schedule ID")
    return _PREFIX + schedule_id


async def _describe(client: Client, schedule_id: str) -> Any:
    try:
        return await client.get_schedule_handle(_identity(schedule_id)).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=404, detail="Schedule not found") from exc
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc


async def _recent_action(client: Client, action: Any) -> dict[str, Any]:
    occurrence = action.action
    result: dict[str, Any] = {
        "scheduled_at": action.scheduled_at,
        "started_at": action.started_at,
        "occurrence_id": occurrence.workflow_id,
    }
    handle = client.get_workflow_handle(
        occurrence.workflow_id, run_id=occurrence.first_execution_run_id
    )
    try:
        execution = await handle.describe()
        if execution.status == WorkflowExecutionStatus.COMPLETED:
            result.update(await handle.result())
        elif execution.status == WorkflowExecutionStatus.RUNNING:
            result["outcome"] = "running"
        else:
            result["outcome"] = "failed"
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc
        result["outcome"] = "history_expired"
    return result


async def _view(client: Client, description: Any) -> dict[str, Any]:
    return {
        "id": description.id.removeprefix(_PREFIX),
        "spec": await description.memo_value("spec"),
        "paused": description.schedule.state.paused,
        "next_executions": description.info.next_action_times,
        "recent_actions": [
            await _recent_action(client, action) for action in description.info.recent_actions
        ],
        "skipped_overlap": description.info.num_actions_skipped_overlap,
        "missed_catchup": description.info.num_actions_missed_catchup_window,
    }


@router.put("/{schedule_id}")
async def create_schedule(
    schedule_id: str,
    spec: ScheduleSpec,
    config: SystemConfig = Depends(get_system_config),
    client: Client = Depends(get_schedule_client),
) -> dict[str, Any]:
    identity = _identity(schedule_id)
    if not config.resolve_repo_key(spec.repo_key):
        try:
            existing = await _describe(client, schedule_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        else:
            if ScheduleSpec.model_validate(await existing.memo_value("spec")) != spec:
                raise HTTPException(status_code=409, detail="Schedule specification conflicts")
            return await _view(client, existing)
        raise HTTPException(status_code=400, detail="Repository key is not allowlisted")
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "ScheduledReviewWorkflow",
            args=[schedule_id, spec.model_dump(mode="json")],
            id=identity,
            task_queue=DEFAULT_TEMPORAL_TASK_QUEUE,
        ),
        spec=spec.temporal_spec(),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(hours=1)
        ),
        state=ScheduleState(paused=True),
    )
    try:
        await client.create_schedule(
            identity, schedule, memo={"spec": spec.model_dump(mode="json")}
        )
    except ScheduleAlreadyRunningError:
        existing = await _describe(client, schedule_id)
        if ScheduleSpec.model_validate(await existing.memo_value("spec")) != spec:
            raise HTTPException(
                status_code=409, detail="Schedule specification conflicts"
            ) from None
        return await _view(client, existing)
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc
    return await _view(client, await _describe(client, schedule_id))


@router.get("")
async def list_schedules(client: Client = Depends(get_schedule_client)) -> list[dict[str, Any]]:
    try:
        iterator = await client.list_schedules()
        ids = [entry.id async for entry in iterator if entry.id.startswith(_PREFIX)]
        return [
            await _view(client, await _describe(client, identity.removeprefix(_PREFIX)))
            for identity in ids
        ]
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc


@router.get("/{schedule_id}")
async def get_schedule(
    schedule_id: str, client: Client = Depends(get_schedule_client)
) -> dict[str, Any]:
    return await _view(client, await _describe(client, schedule_id))


@router.post("/{schedule_id}/{operation}")
async def manage_schedule(
    schedule_id: str, operation: str, client: Client = Depends(get_schedule_client)
) -> dict[str, Any]:
    if operation not in {"pause", "resume", "trigger"}:
        raise HTTPException(status_code=404, detail="Unknown operation")
    await _describe(client, schedule_id)
    handle = client.get_schedule_handle(_identity(schedule_id))
    try:
        if operation == "pause":
            await handle.pause()
        elif operation == "resume":
            await handle.unpause()
        else:
            await handle.trigger(overlap=ScheduleOverlapPolicy.SKIP)
    except RPCError as exc:
        raise HTTPException(status_code=503, detail="Temporal is unavailable") from exc
    return await _view(client, await _describe(client, schedule_id))


@router.get("/{schedule_id}/runs")
async def schedule_runs(
    schedule_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    client: Client = Depends(get_schedule_client),
    service: TaskExecutionService = Depends(get_task_service),
) -> list[dict[str, Any]]:
    await _describe(client, schedule_id)
    with session_scope(service.session_factory) as session:
        found = SessionRepository(session).get_by_channel_thread(
            channel="schedule", external_thread_id=schedule_id
        )
        session_id = found.id if found else None
    if session_id is None:
        return []
    return [
        {
            "task": task.model_dump(mode="json"),
            "status_url": f"/tasks/{task.task_id}",
            "timeline_url": f"/tasks/{task.task_id}",
            "artifacts_url": f"/tasks/{task.task_id}",
        }
        for task in service.list_tasks(session_id=session_id, limit=limit, offset=offset)
    ]
