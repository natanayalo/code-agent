"""Durable, runtime-neutral execution of one decomposed plan node."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Literal, cast

from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from db.base import utc_now
from db.enums import ExecutionPlanNodeStatus
from orchestrator.state import NodeOutcome, OrchestratorModel
from repositories import ExecutionPlanRepository, session_scope
from workers import Worker, WorkerRequest, WorkerResult

NODE_ACTIVITY_SCHEMA_VERSION = 1


class NodeActivityRequest(OrchestratorModel):
    """Compact, deterministic identity for one logical node execution."""

    schema_version: int = NODE_ACTIVITY_SCHEMA_VERSION
    task_id: str
    plan_id: str
    node_id: str
    logical_attempt: int = Field(ge=1)
    logical_activity_key: str
    effective_input_digest: str


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


class NodeExecutionService:
    """Application service; Temporal and legacy execution share this boundary."""

    def __init__(self, session_factory: Callable[[], Session], worker: Worker) -> None:
        self.session_factory = session_factory
        self.worker = worker

    async def execute(
        self,
        *,
        activity: NodeActivityRequest,
        request: WorkerRequest,
        effective_input_summary: dict[str, object],
    ) -> tuple[NodeActivityResultRef, NodeOutcome | None]:
        """Claim, run, and independently persist exactly one logical node result."""
        with session_scope(cast(sessionmaker[Session], self.session_factory)) as session:
            repository = ExecutionPlanRepository(session)
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
                task_trace_id=None,
            )
            if claim == "collision":
                raise ValueError(
                    "logical node activity key was reused with a different input digest"
                )
            if claim == "terminal_replay":
                payload = dict(attempt.result_payload or {})
                result = WorkerResult.model_validate(payload["worker_result"])
                outcome = NodeOutcome.model_validate(payload["node_outcome"])
                return (
                    NodeActivityResultRef(
                        node_id=activity.node_id,
                        logical_activity_key=activity.logical_activity_key,
                        status="terminal_replay",
                        result_digest=attempt.result_digest or _result_digest(payload),
                        continuation=payload.get("continuation", "continue"),
                    ),
                    outcome,
                )
            if claim == "in_progress":
                return (
                    NodeActivityResultRef(
                        node_id=activity.node_id,
                        logical_activity_key=activity.logical_activity_key,
                        status="failed",
                        result_digest="",
                        continuation="retry_node",
                    ),
                    None,
                )
            attempt_id, token = attempt.id, attempt.claim_token

        result = await self.worker.run(request)
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
