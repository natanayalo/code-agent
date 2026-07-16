"""Focused snapshot projection regression tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from orchestrator.execution_snapshot_service import _map_execution_plan_to_snapshot


def test_execution_plan_snapshot_defaults_legacy_fanout_metadata() -> None:
    """Legacy nullable metadata must not invalidate an operator task snapshot."""
    now = datetime.now(UTC)
    node = SimpleNamespace(
        node_id="inspect",
        depends_on=[],
        task_spec=None,
        node_kind="inspect",
        aggregation_role=None,
        execution_mode=None,
        parallel_safe=None,
        status="pending",
        goal="Inspect the repository",
        acceptance_criteria=None,
        assigned_worker_profile=None,
        budget=None,
        validation_commands=None,
        artifacts=None,
        blocker_interaction_id=None,
        retry_count=0,
        started_at=None,
        finished_at=None,
        worker_run_id=None,
        result_summary=None,
        failure_kind=None,
        verification_outcome=None,
        changed_files=None,
        output_artifacts=None,
        last_attempt_at=None,
        attempts=[],
        created_at=now,
        updated_at=now,
    )
    plan = SimpleNamespace(
        id="plan",
        task_id="task",
        created_at=now,
        updated_at=now,
        nodes=[node],
    )

    snapshot = _map_execution_plan_to_snapshot(plan)

    assert snapshot is not None
    assert snapshot.nodes[0].aggregation_role == "mutation"
    assert snapshot.nodes[0].execution_mode == "mutable"
    assert snapshot.nodes[0].parallel_safe is False
