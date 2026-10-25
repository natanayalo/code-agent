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


def test_resolve_task_delivery_metadata_merges_github_constraints() -> None:
    from orchestrator.execution_snapshot_service import _resolve_task_delivery_metadata

    task = SimpleNamespace(
        constraints={
            "github": {
                "issue_number": 42,
                "issue_url": "https://github.com/octocat/code-agent/issues/42",
            },
            "delivery_branch": "agent/task-42",
        },
        worker_runs=[],
    )
    run = SimpleNamespace(
        id="run-1",
        started_at=datetime.now(UTC),
        delivery_metadata={
            "pr_number": 105,
            "pr_url": "https://github.com/octocat/code-agent/pull/105",
            "ci_status": "failed",
            "ci_failed_jobs": ["lint", "test"],
        },
    )

    resolved = _resolve_task_delivery_metadata(task, latest_run=run)
    assert resolved is not None
    assert resolved.pr_number == 105
    assert resolved.source_issue_number == 42
    assert resolved.source_issue_url == "https://github.com/octocat/code-agent/issues/42"
    assert resolved.branch_name == "agent/task-42"
    assert resolved.ci_status == "failed"
    assert resolved.ci_failed_jobs == ["lint", "test"]


def test_resolve_task_delivery_metadata_from_attached_listing_metadata() -> None:
    from orchestrator.execution_snapshot_service import _resolve_task_delivery_metadata

    task = SimpleNamespace(
        constraints={},
        worker_runs=[],
        _latest_run_delivery_metadata={
            "pr_number": 77,
            "ci_status": "passed",
        },
    )

    resolved = _resolve_task_delivery_metadata(task)
    assert resolved is not None
    assert resolved.pr_number == 77
    assert resolved.ci_status == "passed"


def test_resolve_task_delivery_metadata_returns_none_when_empty() -> None:
    from orchestrator.execution_snapshot_service import _resolve_task_delivery_metadata

    task = SimpleNamespace(constraints={}, worker_runs=[])
    resolved = _resolve_task_delivery_metadata(task)
    assert resolved is None


def test_map_task_to_snapshot_enriches_latest_run_delivery_metadata() -> None:
    from orchestrator.execution_snapshot_service import (
        _map_task_to_snapshot,
        _resolve_task_delivery_metadata,
    )
    from orchestrator.execution_types import TaskSummarySnapshot

    now = datetime.now(UTC)
    task = SimpleNamespace(
        id="task-1",
        session_id="session-1",
        task_text="Delivery test",
        status="completed",
        constraints={
            "github": {
                "issue_number": 42,
                "issue_url": "https://github.com/octocat/code-agent/issues/42",
            },
            "delivery_branch": "agent/task-42",
        },
        worker_runs=[
            SimpleNamespace(
                id="run-1",
                session_id="session-1",
                worker_type="codex",
                worker_profile=None,
                runtime_mode=None,
                orchestration_runtime=None,
                workspace_id="ws-1",
                status="success",
                started_at=now,
                finished_at=now,
                summary="Done",
                requested_permission=None,
                budget_usage=None,
                verifier_outcome=None,
                commands_run=[],
                files_changed_count=0,
                files_changed=[],
                artifact_index=[],
                delivery_metadata={
                    "pr_number": 105,
                    "pr_url": "https://github.com/octocat/code-agent/pull/105",
                },
                artifacts=[],
            )
        ],
        task_spec=None,
        execution_plan=None,
        timeline_events=[],
    )
    svc = SimpleNamespace(
        _pending_interaction_snapshots=lambda _t: [],
        _ensure_verifier_outcome_ids=lambda _v: None,
        _map_task_to_summary=lambda _t, latest_run=None: TaskSummarySnapshot(
            task_id="task-1",
            session_id="session-1",
            status="completed",
            task_text="Delivery test",
            created_at=now,
            updated_at=now,
            delivery_metadata=_resolve_task_delivery_metadata(task, latest_run=latest_run),
        ),
    )
    snapshot = _map_task_to_snapshot(svc, task)
    assert snapshot.latest_run is not None
    assert snapshot.latest_run.delivery_metadata is not None
    assert snapshot.latest_run.delivery_metadata.pr_number == 105
    assert snapshot.latest_run.delivery_metadata.source_issue_number == 42
    assert (
        snapshot.latest_run.delivery_metadata.source_issue_url
        == "https://github.com/octocat/code-agent/issues/42"
    )
    assert snapshot.latest_run.delivery_metadata.branch_name == "agent/task-42"


def test_delivery_metadata_snapshot_retains_review_comments_and_pr_title() -> None:
    from orchestrator.execution_snapshot_service import _resolve_task_delivery_metadata
    from orchestrator.execution_types import DeliveryMetadataSnapshot

    now = datetime.now(UTC)
    raw = {
        "pr_title": "Fix memory leak (#42)",
        "pr_number": 105,
        "pr_url": "https://github.com/octocat/code-agent/pull/105",
        "review_comments": [
            {
                "id": 901,
                "path": "src/app.py",
                "line": 42,
                "body": "Improve error handling",
                "user_login": "reviewer1",
                "created_at": now.isoformat(),
                "replied": True,
            }
        ],
        "review_comments_last_checked_at": now.isoformat(),
        "review_comments_replied_at": now.isoformat(),
    }

    snapshot = DeliveryMetadataSnapshot.model_validate(raw)
    assert snapshot.pr_title == "Fix memory leak (#42)"
    assert len(snapshot.review_comments) == 1
    assert snapshot.review_comments[0]["id"] == 901
    assert snapshot.review_comments[0]["replied"] is True
    assert snapshot.review_comments_last_checked_at == now
    assert snapshot.review_comments_replied_at == now

    task = SimpleNamespace(constraints={}, worker_runs=[])
    run = SimpleNamespace(id="r1", started_at=now, delivery_metadata=raw)
    resolved = _resolve_task_delivery_metadata(task, latest_run=run)
    assert resolved is not None
    assert resolved.pr_title == "Fix memory leak (#42)"
    assert len(resolved.review_comments) == 1
    assert resolved.review_comments_last_checked_at == now
