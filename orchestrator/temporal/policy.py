"""Explicit lifecycle policies for Temporal activities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio.common import RetryPolicy


@dataclass(frozen=True, slots=True)
class TemporalActivityPolicy:
    """Retry and timeout contract for one Temporal activity type."""

    start_to_close_timeout: timedelta
    retry_policy: RetryPolicy
    heartbeat_timeout: timedelta | None = None


_STANDARD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)
_WORKER_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_attempts=2,
)
_POLICIES: dict[str, TemporalActivityPolicy] = {
    "classify_and_plan": TemporalActivityPolicy(timedelta(minutes=5), _STANDARD_RETRY),
    "decompose_task": TemporalActivityPolicy(timedelta(minutes=5), _STANDARD_RETRY),
    "load_memory": TemporalActivityPolicy(timedelta(minutes=5), _STANDARD_RETRY),
    "provision_workspace": TemporalActivityPolicy(timedelta(minutes=10), _STANDARD_RETRY),
    "run_worker": TemporalActivityPolicy(
        timedelta(minutes=30),
        _WORKER_RETRY,
        heartbeat_timeout=timedelta(seconds=20),
    ),
    "select_next_node": TemporalActivityPolicy(timedelta(minutes=5), _STANDARD_RETRY),
    "run_decomposed_node": TemporalActivityPolicy(
        timedelta(minutes=30), _WORKER_RETRY, heartbeat_timeout=timedelta(seconds=20)
    ),
    "merge_node_wave": TemporalActivityPolicy(timedelta(minutes=5), _STANDARD_RETRY),
    "fail_node_permission_escalation": TemporalActivityPolicy(
        timedelta(minutes=5), _STANDARD_RETRY
    ),
    "request_permission_escalation": TemporalActivityPolicy(timedelta(minutes=5), _STANDARD_RETRY),
    "resolve_permission_escalation": TemporalActivityPolicy(timedelta(minutes=5), _STANDARD_RETRY),
    "verify_result": TemporalActivityPolicy(timedelta(minutes=15), _STANDARD_RETRY),
    "deliver_result": TemporalActivityPolicy(timedelta(minutes=10), _STANDARD_RETRY),
    "persist_memory": TemporalActivityPolicy(timedelta(minutes=5), _STANDARD_RETRY),
    "record_workflow_failure": TemporalActivityPolicy(
        timedelta(minutes=5),
        RetryPolicy(maximum_attempts=3),
    ),
}

EXECUTION_ACTIVITIES = frozenset({"run_worker", "run_decomposed_node"})


def activity_options(activity_name: str, *, task_queue: str | None = None) -> dict[str, Any]:
    """Return Temporal execution options for a named activity.

    Passing a queue is only valid for the worker activity, which is routed by
    the selected worker profile. Every other activity stays on the workflow's
    orchestration queue.
    """
    policy = _POLICIES.get(activity_name)
    if policy is None:
        raise ValueError(f"Unknown Temporal activity policy: {activity_name}")
    if task_queue is not None and activity_name not in EXECUTION_ACTIVITIES:
        raise ValueError(
            f"Only execution activities may select a Temporal task queue: {activity_name}"
        )

    options: dict[str, Any] = {
        "start_to_close_timeout": policy.start_to_close_timeout,
        "retry_policy": policy.retry_policy,
    }
    if policy.heartbeat_timeout is not None:
        options["heartbeat_timeout"] = policy.heartbeat_timeout
    if task_queue is not None:
        options["task_queue"] = task_queue
    return options
