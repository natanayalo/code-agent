"""Unit tests for provider reliability stage inspection and failure resolution."""

from __future__ import annotations

from datetime import UTC, datetime

from db.enums import ArtifactType, TaskStatus, TimelineEventType, WorkerRunStatus
from db.models import Task, TaskTimelineEvent, WorkerRun
from evaluation.provider_reliability_stages import (
    check_delivery_stage,
    check_review_stage,
    check_verification_stage,
    resolve_failure_kind,
    verify_timeline_consistency,
)


def test_provider_reliability_stages_direct() -> None:
    """Direct unit tests for stage checking, artifact extraction, and failure kinds."""
    consistent, ts = verify_timeline_consistency(TaskStatus.IN_PROGRESS, [], None)
    assert not consistent and ts is None

    consistent, ts = verify_timeline_consistency(TaskStatus.FAILED, [], None)
    assert not consistent and ts is None

    task = Task(status=TaskStatus.COMPLETED, task_spec={"verification_commands": ["test"]})
    run_passed = WorkerRun(status=WorkerRunStatus.SUCCESS, verifier_outcome={"status": "passed"})
    run_failed = WorkerRun(status=WorkerRunStatus.FAILURE, verifier_outcome={"status": "failed"})
    assert check_verification_stage(task, [run_passed]) == (True, True)
    assert check_verification_stage(task, [run_failed]) == (True, False)

    run_with_review_pass = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        artifact_index=[
            {
                "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
                "artifact_metadata": {
                    ArtifactType.INDEPENDENT_REVIEW_RESULT.value: {
                        "outcome": "no_findings",
                        "findings": [],
                    }
                },
            }
        ],
    )
    assert check_review_stage(task, [run_with_review_pass], v_passed=True) == (True, True)

    run_with_review_fail = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        artifact_index=[
            {
                "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
                "artifact_metadata": {
                    ArtifactType.INDEPENDENT_REVIEW_RESULT.value: {
                        "outcome": "findings",
                        "findings": [{"message": "bug"}],
                    }
                },
            }
        ],
    )
    assert check_review_stage(task, [run_with_review_fail], v_passed=True) == (True, False)

    task_repair = Task(
        status=TaskStatus.COMPLETED,
        task_spec={"allowed_actions": ["modify_workspace_files"]},
        constraints={"independent_review_repair_passes_used": 1},
    )
    assert check_review_stage(task_repair, [], v_passed=True) == (False, False)

    run_delivery = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        delivery_metadata={"branch": "refs/heads/feature"},
    )
    task_branch = Task(status=TaskStatus.COMPLETED, task_spec={"delivery_mode": "branch"})
    assert check_delivery_stage(task_branch, [run_delivery]) == (True, False)

    task_err = Task(status=TaskStatus.FAILED, last_error="boom")
    assert resolve_failure_kind(task_err, accepted=False, runs=[]) == "task_error"
    task_unknown = Task(status=TaskStatus.FAILED, last_error=None)
    assert resolve_failure_kind(task_unknown, accepted=False, runs=[]) == "unknown"


def test_terminal_delivery_failed_overrides_verifier_outcome_and_ignores_stale_attempts() -> None:
    """Proves current-attempt timeline failure overrides verifier; stale attempts ignored."""
    now = datetime.now(UTC)

    # Case 1: Current-attempt DELIVERY_FAILED overrides compile verifier outcome
    task_current = Task(
        status=TaskStatus.FAILED,
        attempt_count=1,
        timeline_events=[
            TaskTimelineEvent(
                attempt_number=1,
                sequence_number=1,
                event_type=TimelineEventType.DELIVERY_FAILED,
                payload={"failure_kind": "infra_verifier_unavailable"},
                created_at=now,
            )
        ],
    )
    run_with_compile_failure = WorkerRun(
        status=WorkerRunStatus.FAILURE,
        verifier_outcome={"status": "failed", "failure_kind": "compile"},
    )
    assert (
        resolve_failure_kind(task_current, accepted=False, runs=[run_with_compile_failure])
        == "infra_verifier_unavailable"
    )

    # Case 2: Stale attempt 1 DELIVERY_FAILED is ignored when current attempt is 2
    task_stale = Task(
        status=TaskStatus.FAILED,
        attempt_count=2,
        timeline_events=[
            TaskTimelineEvent(
                attempt_number=1,
                sequence_number=1,
                event_type=TimelineEventType.DELIVERY_FAILED,
                payload={"failure_kind": "infra_verifier_unavailable"},
                created_at=now,
            )
        ],
    )
    # Stale attempt 1 event ignored, so falls back to worker run outcome 'compile'
    assert (
        resolve_failure_kind(task_stale, accepted=False, runs=[run_with_compile_failure])
        == "compile"
    )

    # Case 3: Accepted tasks always return None failure kind
    assert (
        resolve_failure_kind(task_current, accepted=True, runs=[run_with_compile_failure]) is None
    )
