"""Shared utilities for orchestrator nodes."""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from db.base import utc_now
from db.enums import TimelineEventType
from orchestrator.constants import COMPLEX_TASK_MARKERS
from orchestrator.state import OrchestratorState, TaskTimelineEventState
from workers import Worker, WorkerRequest, WorkerResult, WorkerType

logger = logging.getLogger(__name__)

CODEX_WORKER: Final[WorkerType] = "codex"
GEMINI_WORKER: Final[WorkerType] = "gemini"
OPENROUTER_WORKER: Final[WorkerType] = "openrouter"

_COMPLEX_TASK_PATTERN = re.compile(
    rf"(?<![\w-])(?:{'|'.join(re.escape(marker) for marker in COMPLEX_TASK_MARKERS)})(?![\w-])"
)


def _default_worker_result_provider(request: WorkerRequest) -> WorkerResult:
    """Return a fake successful worker result for the skeleton happy path."""
    return WorkerResult(
        status="success",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="persist_memory",
        summary=f"Fake worker completed: {request.task_text}",
    )


class _DefaultFakeWorker(Worker):
    """Fallback worker used until a real provider-specific adapter exists."""

    async def run(
        self,
        request: WorkerRequest,
        *,
        system_prompt: str | None = None,
    ) -> WorkerResult:
        return _default_worker_result_provider(request)


def _available_workers(
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
    shell_worker: Worker | None = None,
) -> dict[str, Worker]:
    """Return the workers that are actually wired into the graph."""
    result: dict[str, Worker] = {CODEX_WORKER: worker or _DefaultFakeWorker()}
    if gemini_worker is not None:
        result[GEMINI_WORKER] = gemini_worker
    if openrouter_worker is not None:
        result[OPENROUTER_WORKER] = openrouter_worker
    if shell_worker is not None:
        result["shell"] = shell_worker
    return result


def _ensure_state(state: OrchestratorState | dict[str, Any]) -> OrchestratorState:
    """Normalize raw graph input into the typed orchestrator state."""
    if isinstance(state, OrchestratorState):
        return state
    return OrchestratorState.model_validate(state)


def _progress_update(state: OrchestratorState, message: str) -> list[str]:
    """Append a progress message while preserving prior updates."""
    return [*state.progress_updates, message]


def _timeline_events(
    state: OrchestratorState,
    *events: tuple[TimelineEventType, str | None, dict[str, Any] | None],
) -> dict[str, Any]:
    """Create one or more structured timeline events for state merging."""
    last_event = next(
        (e for e in reversed(state.timeline_events) if e.attempt_number == state.attempt_count),
        None,
    )
    if last_event:
        base_seq = last_event.sequence_number + 1
    else:
        base_seq = state.timeline_persisted_count

    now = utc_now()

    return {
        "timeline_events": [
            TaskTimelineEventState(
                event_type=str(etype),
                attempt_number=state.attempt_count,
                sequence_number=base_seq + i,
                message=msg,
                payload=payload,
                created_at=now,
            )
            for i, (etype, msg, payload) in enumerate(events)
        ],
    }


def _timeline_event(
    state: OrchestratorState,
    event_type: TimelineEventType,
    *,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shorthand for a single timeline event emission."""
    return _timeline_events(state, (event_type, message, payload))


def _classify_task_kind(task_text: str) -> str:
    """Apply a small heuristic classifier for the workflow skeleton."""
    normalized_text = task_text.lower()
    if any(keyword in normalized_text for keyword in ("refactor", "architecture", "design")):
        return "architecture"
    if any(
        keyword in normalized_text
        for keyword in ("investigate", "debug", "analyze", "review", "audit", "compare")
    ):
        return "ambiguous"
    return "implementation"


def _task_complexity_reason(state: OrchestratorState) -> str | None:
    """Return a reason when the task should receive a structured plan."""
    task_kind = state.task_kind
    if task_kind == "architecture":
        return "architectural_task"
    if task_kind == "ambiguous":
        return "ambiguous_task"
    task_text = (state.normalized_task_text or state.task.task_text).lower()
    if _COMPLEX_TASK_PATTERN.search(task_text):
        return "multi_file_task"
    return None


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    """Return unique values while preserving first-seen ordering."""
    return list(dict.fromkeys(values))


def _requires_deliverable_evidence(state: OrchestratorState) -> bool:
    """Return whether this task should fail when no concrete deliverable is produced."""
    task_type = state.task_spec.task_type if state.task_spec is not None else None
    # For now, only implementation-heavy tasks strictly require file/artifact changes.
    # Pure investigations or reviews might only result in a summary.
    # bugfix and refactor tasks strongly imply file changes.
    if task_type in {"bugfix", "refactor"}:
        return True

    task_text = (state.normalized_task_text or state.task.task_text).lower()
    # Strong implementation keywords that should almost always result in a file change.
    implementation_keywords = (
        "fix",
        "implement",
        "patch",
        "refactor",
        "modify",
        "remove",
        "delete",
    )
    return any(keyword in task_text for keyword in implementation_keywords)


def _has_meaningful_deliverable(state: OrchestratorState) -> bool:
    """Return True if worker output contains concrete deliverable signals."""
    if state.result is None:
        return False
    if state.result.files_changed:
        return True
    if state.result.diff_text:
        return True
    if state.result.json_payload:
        return True

    # T-117: Substantial summaries also count as deliverables for informative tasks
    if state.result.summary and len(state.result.summary) > 100:
        return True

    non_log_artifacts = [
        artifact
        for artifact in state.result.artifacts
        if (artifact.artifact_type or "").lower() not in {"log", "workspace"}
    ]
    return bool(non_log_artifacts)
