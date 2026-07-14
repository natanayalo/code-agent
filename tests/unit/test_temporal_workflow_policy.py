from datetime import timedelta

from orchestrator.temporal.policy import activity_options


def test_worker_activity_policy_has_bounded_retry_and_heartbeat() -> None:
    """Long-running worker work must retain explicit recovery bounds."""
    options = activity_options("run_worker", task_queue="code-agent-codex")

    assert options["start_to_close_timeout"] == timedelta(minutes=30)
    assert options["heartbeat_timeout"] == timedelta(seconds=20)
    assert options["task_queue"] == "code-agent-codex"
    retry_policy = options["retry_policy"]
    assert retry_policy.maximum_attempts == 2
    assert retry_policy.initial_interval == timedelta(seconds=5)


def test_projection_failure_policy_is_bounded_and_does_not_use_a_worker_queue() -> None:
    """Terminal failure projection is retried on the orchestration queue only."""
    options = activity_options("record_workflow_failure")

    assert options["start_to_close_timeout"] == timedelta(minutes=5)
    assert "heartbeat_timeout" not in options
    assert "task_queue" not in options
    retry_policy = options["retry_policy"]
    assert retry_policy.maximum_attempts == 3


def test_unknown_activity_policy_is_rejected() -> None:
    """A new workflow activity must declare its lifecycle policy explicitly."""
    try:
        activity_options("unknown")
    except ValueError as exc:
        assert str(exc) == "Unknown Temporal activity policy: unknown"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("Unknown activity policy unexpectedly resolved.")
