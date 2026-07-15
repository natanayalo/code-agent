"""Temporal queue names and profile-to-capability routing."""

from __future__ import annotations

DEFAULT_TEMPORAL_TASK_QUEUE = "task-execution-queue"
CODEX_EXECUTION_TASK_QUEUE = "code-agent-codex"


def execution_task_queue_for_profile(profile_name: str | None) -> str:
    """Return the execution queue for a routed worker profile.

    Queues represent worker capability classes. Product-policy details remain in
    ``WorkerProfile`` rather than creating a queue for every profile variant.
    """
    if profile_name and profile_name.strip().lower().startswith("codex-"):
        return CODEX_EXECUTION_TASK_QUEUE
    return DEFAULT_TEMPORAL_TASK_QUEUE
