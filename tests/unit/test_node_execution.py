"""Contract tests for durable node execution identities."""

import asyncio

import pytest

from orchestrator.node_execution import (
    NodeActivityClaimLost,
    NodeActivityRequest,
    _execute_worker_under_claim,
    _legacy_terminal_outcome,
    logical_activity_key,
)
from workers import WorkerResult


def test_node_activity_request_requires_canonical_identity_and_digest() -> None:
    plan_id = "plan"
    request = NodeActivityRequest(
        task_id="task",
        plan_id=plan_id,
        node_id="node",
        logical_attempt=1,
        logical_activity_key=logical_activity_key(plan_id, "node", 1),
        effective_input_digest="a" * 64,
    )

    assert request.schema_version == 1


def test_node_activity_request_rejects_malformed_identity() -> None:
    with pytest.raises(ValueError, match="logical_activity_key"):
        NodeActivityRequest(
            task_id="task",
            plan_id="plan",
            node_id="node",
            logical_attempt=1,
            logical_activity_key="wrong",
            effective_input_digest="a" * 64,
        )


def test_legacy_terminal_outcome_preserves_permission_continuation() -> None:
    result, outcome, continuation = _legacy_terminal_outcome(
        node_id="node",
        logical_attempt=2,
        status="blocked",
        failure_kind="permission_denied",
    )

    assert result.status == "failure"
    assert result.failure_kind == "permission_denied"
    assert outcome.status == "blocked"
    assert outcome.attempts == 2
    assert continuation == "await_permission"


def test_lost_claim_cancels_worker_before_processing_result() -> None:
    worker_cancelled = asyncio.Event()

    async def worker() -> WorkerResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            worker_cancelled.set()
            raise
        raise AssertionError("cancelled worker unexpectedly completed")

    async def lost_claim() -> bool:
        return False

    async def exercise() -> None:
        with pytest.raises(NodeActivityClaimLost):
            await _execute_worker_under_claim(worker, lost_claim)

    asyncio.run(exercise())
    assert worker_cancelled.is_set()
