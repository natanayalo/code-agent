"""Durable, runtime-neutral execution of one decomposed plan node."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Literal, cast

from pydantic import Field, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from db.base import utc_now
from db.enums import ExecutionPlanNodeStatus
from orchestrator.state import NodeOutcome, OrchestratorModel
from repositories import ExecutionPlanRepository, session_scope
from workers import FailureKind, WorkerRequest, WorkerResult

NODE_ACTIVITY_SCHEMA_VERSION = 1
CLAIM_HEARTBEAT_SECONDS = 10
CLAIM_LEASE_SECONDS = 60
logger = logging.getLogger(__name__)


class NodeActivityInProgress(RuntimeError):
    """Raised when another live execution owns the logical node activity."""


class NodeActivityClaimLost(RuntimeError):
    """Raised when execution loses its durable claim before completion."""


class NodeActivityRequest(OrchestratorModel):
    """Compact, deterministic identity for one logical node execution."""

    schema_version: Literal[1] = 1
    task_id: str
    plan_id: str
    node_id: str
    logical_attempt: int = Field(ge=1)
    logical_activity_key: str
    effective_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_trace_id: str | None = None
    execution_capacity_key: str | None = None

    @model_validator(mode="after")
    def validate_logical_activity_key(self) -> NodeActivityRequest:
        """Reject caller-supplied identities that do not match the node contract."""
        expected = logical_activity_key(self.plan_id, self.node_id, self.logical_attempt)
        if self.logical_activity_key != expected:
            raise ValueError("logical_activity_key does not match the plan node attempt")
        return self


class NodeActivityResultRef(OrchestratorModel):
    """Small result safe to place in Temporal workflow history."""

    schema_version: int = NODE_ACTIVITY_SCHEMA_VERSION
    node_id: str
    logical_activity_key: str
    status: Literal["completed", "failed", "blocked", "cancelled", "terminal_replay"]
    result_digest: str
    continuation: Literal["continue", "retry_node", "await_permission", "fail_task"]


def logical_activity_key(plan_id: str, node_id: str, logical_attempt: int) -> str:
    """Generate the sole accepted logical activity identity format."""
    return f"node-activity:v1:{plan_id}:{node_id}:{logical_attempt}"


def _result_digest(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _node_status(result: WorkerResult) -> tuple[str, str]:
    if result.status == "success":
        return "completed", "continue"
    if result.next_action_hint == "request_higher_permission":
        return "blocked", "await_permission"
    return "failed", "retry_node"


def _legacy_terminal_outcome(
    *,
    node_id: str,
    logical_attempt: int,
    status: str,
    failure_kind: str | None,
) -> tuple[WorkerResult, NodeOutcome, str]:
    """Reconstruct a safe outcome for a terminal attempt from before M25.1."""
    outcome_status: Literal["completed", "blocked", "failed"] = (
        "completed" if status == "completed" else "blocked" if status == "blocked" else "failed"
    )
    worker_status: Literal["success", "failure"] = (
        "success" if outcome_status == "completed" else "failure"
    )
    try:
        result = WorkerResult(
            status=worker_status,
            summary=failure_kind or f"Legacy node attempt {status}",
            failure_kind=cast(
                FailureKind | None,
                failure_kind or (None if worker_status == "success" else "unknown"),
            ),
            next_action_hint=("request_higher_permission" if outcome_status == "blocked" else None),
        )
    except ValueError:
        result = WorkerResult(
            status=worker_status,
            summary=f"Legacy node attempt {status}",
            failure_kind=None if worker_status == "success" else "unknown",
            next_action_hint=("request_higher_permission" if outcome_status == "blocked" else None),
        )
    outcome = NodeOutcome(
        node_id=node_id,
        status=cast(Literal["completed", "failed", "blocked", "skipped"], outcome_status),
        result=result,
        attempts=logical_attempt,
    )
    return result, outcome, _node_status(result)[1]


async def _execute_worker_under_claim(
    execute_worker: Callable[[], Awaitable[WorkerResult]],
    heartbeat_claim: Callable[[], Awaitable[bool]],
) -> WorkerResult:
    """Cancel worker execution if its durable claim stops being owned."""
    worker_task: asyncio.Future[WorkerResult] = asyncio.ensure_future(execute_worker())
    heartbeat_task: asyncio.Future[bool] = asyncio.ensure_future(heartbeat_claim())
    try:
        done, _pending = await asyncio.wait(
            [
                cast(asyncio.Future[object], worker_task),
                cast(asyncio.Future[object], heartbeat_task),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done and not heartbeat_task.result():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            raise NodeActivityClaimLost("node activity claim was lost during worker execution")
        return worker_task.result()
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)


class NodeExecutionService:
    """Application service; Temporal and legacy execution share this boundary."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    async def execute(
        self,
        *,
        activity: NodeActivityRequest,
        request: WorkerRequest,
        effective_input_summary: dict[str, object],
        execute_worker: Callable[[], Awaitable[WorkerResult]],
    ) -> tuple[NodeActivityResultRef, NodeOutcome | None]:
        """Claim, run, and independently persist exactly one logical node result."""
        with session_scope(cast(sessionmaker[Session], self.session_factory)) as session:
            repository = ExecutionPlanRepository(session)
            plan = repository.get_by_id(activity.plan_id)
            if plan is None or plan.task_id != activity.task_id:
                raise ValueError("node activity plan does not belong to the task")
            claim, attempt = repository.claim_activity(
                plan_id=activity.plan_id,
                node_id=activity.node_id,
                logical_activity_key=activity.logical_activity_key,
                effective_input_summary=effective_input_summary,
                effective_input_digest=activity.effective_input_digest,
                worker_type=str(request.worker_type) if request.worker_type else None,
                worker_profile=request.worker_profile,
                runtime_mode=str(request.runtime_mode) if request.runtime_mode else None,
                workspace_id=(
                    request.runtime_manifest.get("workspace_id")
                    if request.runtime_manifest
                    else None
                ),
                task_trace_id=activity.task_trace_id,
                lease_seconds=CLAIM_LEASE_SECONDS,
            )
            if claim == "collision":
                raise ValueError(
                    "logical node activity key was reused with a different input digest"
                )
            if claim == "terminal_replay":
                payload = dict(attempt.result_payload or {})
                if payload:
                    result = WorkerResult.model_validate(payload["worker_result"])
                    outcome = NodeOutcome.model_validate(payload["node_outcome"])
                    continuation = payload.get("continuation", "continue")
                    replay_digest = attempt.result_digest or _result_digest(payload)
                else:
                    result, outcome, continuation = _legacy_terminal_outcome(
                        node_id=activity.node_id,
                        logical_attempt=activity.logical_attempt,
                        status=attempt.status,
                        failure_kind=attempt.failure_kind,
                    )
                    replay_digest = _result_digest(
                        {
                            "schema_version": 0,
                            "legacy_status": attempt.status,
                            "failure_kind": result.failure_kind,
                        }
                    )
                return (
                    NodeActivityResultRef(
                        node_id=activity.node_id,
                        logical_activity_key=activity.logical_activity_key,
                        status="terminal_replay",
                        result_digest=replay_digest,
                        continuation=continuation,
                    ),
                    outcome,
                )
            if claim == "in_progress":
                raise NodeActivityInProgress(activity.logical_activity_key)
            attempt_id, token = attempt.id, attempt.claim_token

        result = await _execute_worker_under_claim(
            execute_worker,
            lambda: self._heartbeat_claim(attempt_id, token),
        )
        if result is None:
            result = WorkerResult(
                status="failure",
                summary="Node execution returned no result.",
                failure_kind="worker_failure",
            )
        status, continuation = _node_status(result)
        outcome = NodeOutcome(
            node_id=activity.node_id,
            status=cast(Literal["completed", "failed", "blocked", "skipped"], status),
            result=result,
            attempts=activity.logical_attempt,
        )
        payload = {
            "schema_version": NODE_ACTIVITY_SCHEMA_VERSION,
            "worker_result": result.model_dump(mode="json"),
            "node_outcome": outcome.model_dump(mode="json"),
            "continuation": continuation,
        }
        digest = _result_digest(payload)
        with session_scope(cast(sessionmaker[Session], self.session_factory)) as session:
            repository = ExecutionPlanRepository(session)
            finished = repository.finish_attempt(
                attempt_id=attempt_id,
                claim_token=token,
                status=status,
                failure_kind=result.failure_kind,
                workspace_id=result.workspace_id,
                result_payload=payload,
                result_schema_version=NODE_ACTIVITY_SCHEMA_VERSION,
                result_digest=digest,
            )
            if finished is None:
                raise RuntimeError("node activity claim was superseded before terminal persistence")
            repository.update_node(
                plan_id=activity.plan_id,
                node_id=activity.node_id,
                status=ExecutionPlanNodeStatus(status),
                result_summary=result.summary,
                failure_kind=result.failure_kind,
                retry_count=max(activity.logical_attempt - 1, 0),
                finished_at=utc_now(),
                last_attempt_at=utc_now(),
                latest_logical_activity_key=activity.logical_activity_key,
                terminal_result_schema_version=NODE_ACTIVITY_SCHEMA_VERSION,
                terminal_result_digest=digest,
                terminal_result_payload=payload,
            )
        return (
            NodeActivityResultRef(
                node_id=activity.node_id,
                logical_activity_key=activity.logical_activity_key,
                status=cast(
                    Literal["completed", "failed", "blocked", "cancelled", "terminal_replay"],
                    status,
                ),
                result_digest=digest,
                continuation=cast(
                    Literal["continue", "retry_node", "await_permission", "fail_task"],
                    continuation,
                ),
            ),
            outcome,
        )

    async def _heartbeat_claim(self, attempt_id: str, claim_token: str | None) -> bool:
        """Keep the database lease live without holding a transaction during work."""
        if not claim_token:
            return False
        while True:
            await asyncio.sleep(CLAIM_HEARTBEAT_SECONDS)
            try:
                with session_scope(cast(sessionmaker[Session], self.session_factory)) as session:
                    owned = ExecutionPlanRepository(session).heartbeat_activity(
                        attempt_id=attempt_id,
                        claim_token=claim_token,
                        lease_seconds=CLAIM_LEASE_SECONDS,
                    )
            except SQLAlchemyError:
                logger.warning(
                    "Node execution heartbeat failed transiently for attempt %s",
                    attempt_id,
                    exc_info=True,
                )
                continue
            if not owned:
                logger.warning("Node execution heartbeat lost ownership of attempt %s", attempt_id)
                return False
