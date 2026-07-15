"""Unit coverage for Temporal execution queue selection."""

from orchestrator.temporal.queues import (
    CODEX_EXECUTION_TASK_QUEUE,
    DEFAULT_TEMPORAL_TASK_QUEUE,
    execution_task_queue_for_profile,
)


def test_codex_profiles_use_the_codex_execution_queue() -> None:
    assert execution_task_queue_for_profile("codex-native-executor") == CODEX_EXECUTION_TASK_QUEUE


def test_unknown_or_absent_profiles_use_the_default_execution_queue() -> None:
    assert execution_task_queue_for_profile(None) == DEFAULT_TEMPORAL_TASK_QUEUE
    assert execution_task_queue_for_profile("antigravity-native-executor") == (
        DEFAULT_TEMPORAL_TASK_QUEUE
    )
