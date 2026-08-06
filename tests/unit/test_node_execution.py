"""Contract tests for durable node execution identities."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from db.enums import WorkerType
from orchestrator.node_execution import (
    NodeActivityInProgress,
    NodeActivityRequest,
    NodeExecutionService,
    _legacy_terminal_outcome,
    _node_status,
    logical_activity_key,
)
from workers import WorkerRequest, WorkerResult


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


def test_node_status_helper():
    r_succ = WorkerResult(status="success")
    assert _node_status(r_succ) == ("completed", "continue")

    r_perm = WorkerResult(status="failure", next_action_hint="request_higher_permission")
    assert _node_status(r_perm) == ("blocked", "await_permission")

    r_fail = WorkerResult(status="failure")
    assert _node_status(r_fail) == ("failed", "retry_node")


@pytest.mark.asyncio
async def test_node_execution_service_execute_branches():
    session_factory = MagicMock()
    sess = MagicMock()
    session_factory.return_value.__enter__.return_value = sess

    service = NodeExecutionService(session_factory)

    activity = NodeActivityRequest(
        task_id="t1",
        plan_id="p1",
        node_id="n1",
        logical_attempt=1,
        logical_activity_key=logical_activity_key("p1", "n1", 1),
        effective_input_digest="a" * 64,
    )
    request = WorkerRequest(task_text="text", repo_url="url", worker_type=WorkerType.CODEX)

    # Collision branch
    with patch("orchestrator.node_execution.ExecutionPlanRepository") as mock_repo_cls:
        mock_repo_cls.return_value.get_by_id.return_value = MagicMock(task_id="t1")
        mock_repo_cls.return_value.claim_activity.return_value = ("collision", None)
        with pytest.raises(ValueError, match="logical node activity key was reused"):
            await service.execute(
                activity=activity,
                request=request,
                effective_input_summary={},
                execute_worker=AsyncMock(),
            )

    # In progress branch
    with patch("orchestrator.node_execution.ExecutionPlanRepository") as mock_repo_cls:
        mock_repo_cls.return_value.get_by_id.return_value = MagicMock(task_id="t1")
        mock_repo_cls.return_value.claim_activity.return_value = ("in_progress", None)
        with pytest.raises(NodeActivityInProgress):
            await service.execute(
                activity=activity,
                request=request,
                effective_input_summary={},
                execute_worker=AsyncMock(),
            )

    # Terminal replay branch
    with patch("orchestrator.node_execution.ExecutionPlanRepository") as mock_repo_cls:
        attempt = MagicMock()
        attempt.status = "completed"
        attempt.failure_kind = None
        attempt.result_payload = None
        attempt.result_digest = None
        mock_repo_cls.return_value.get_by_id.return_value = MagicMock(task_id="t1")
        mock_repo_cls.return_value.claim_activity.return_value = ("terminal_replay", attempt)
        ref, outcome = await service.execute(
            activity=activity,
            request=request,
            effective_input_summary={},
            execute_worker=AsyncMock(),
        )
        assert ref.status == "terminal_replay"
