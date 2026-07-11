"""Build orchestrator graph input from a submission and durable context."""

from __future__ import annotations

from typing import Any

from orchestrator.execution_context import _PersistedTaskContext
from orchestrator.execution_types import TaskSubmission
from orchestrator.state import SessionRef


def build_orchestrator_graph_input(
    submission: TaskSubmission,
    persisted: _PersistedTaskContext,
    effective_budget: dict[str, Any],
    timeline_persisted_count: int,
) -> dict[str, Any]:
    """Assemble graph state from the current submission and durable task context."""
    return {
        "session": SessionRef(
            session_id=persisted.session_id,
            user_id=persisted.user_id,
            channel=persisted.channel,
            external_thread_id=persisted.external_thread_id,
            active_task_id=persisted.task_id,
            status="active",
        ).model_dump(),
        "task": {
            "task_id": persisted.task_id,
            "task_text": submission.task_text,
            "repo_url": submission.repo_url,
            "branch": submission.branch,
            "priority": submission.priority,
            "worker_override": (
                submission.worker_override.value if submission.worker_override is not None else None
            ),
            "worker_profile_override": submission.worker_profile_override,
            "constraints": dict(submission.constraints),
            "budget": effective_budget,
            "secrets": dict(submission.secrets),
            "tools": submission.tools,
        },
        "task_spec": persisted.task_spec,
        "attempt_count": persisted.attempt_count,
        "dispatch": persisted.last_run_dispatch or {},
        "result": persisted.last_run_result,
        "decomposed_plan": persisted.decomposed_plan,
        "node_outcomes": persisted.node_outcomes,
        "timeline_events": persisted.timeline_events,
        "timeline_persisted_count": timeline_persisted_count,
    }
