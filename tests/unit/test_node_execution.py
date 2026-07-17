"""Contract tests for durable node execution identities."""

import pytest

from orchestrator.node_execution import NodeActivityRequest, logical_activity_key


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
