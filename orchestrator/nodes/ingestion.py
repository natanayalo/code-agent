"""Nodes for task ingestion, classification, and initial planning."""

from __future__ import annotations

import logging
from typing import Any

from apps.observability import (
    ATTR_TASK_KIND,
    SPAN_KIND_CHAIN,
    set_current_span_attribute,
    set_span_input_output,
    start_optional_span,
)
from db.enums import TimelineEventType
from orchestrator.nodes.utils import (
    _classify_task_kind,
    _ensure_state,
    _progress_update,
    _task_complexity_reason,
    _timeline_event,
)
from orchestrator.state import OrchestratorState, TaskPlan, TaskPlanStep

logger = logging.getLogger(__name__)

# [Moved to orchestrator/nodes/utils.py]


def ingest_task(state_input: OrchestratorState) -> dict[str, Any]:
    """Normalize the incoming task text before classification."""
    state = _ensure_state(state_input)
    normalized_task_text = state.task.task_text.strip()
    return {
        "current_step": "ingest_task",
        "normalized_task_text": normalized_task_text,
        "progress_updates": _progress_update(state, "task ingested"),
        **_timeline_event(
            state,
            TimelineEventType.TASK_INGESTED,
            message="Task text normalized.",
        ),
    }


def classify_task(state_input: OrchestratorState) -> dict[str, Any]:
    """Classify the task into a coarse workflow category."""
    state = _ensure_state(state_input)
    with start_optional_span(
        tracer_name="orchestrator.graph",
        span_name="orchestrator.node.classify_task",
        attributes={"openinference.span.kind": SPAN_KIND_CHAIN},
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
    ):
        task_text = state.normalized_task_text or state.task.task_text
        task_kind = _classify_task_kind(task_text)
        set_current_span_attribute(ATTR_TASK_KIND, task_kind)
        set_span_input_output(input_data=task_text, output_data={"task_kind": task_kind})
        return {
            "current_step": "classify_task",
            "task_kind": task_kind,
            "progress_updates": _progress_update(state, f"task classified as {task_kind}"),
            **_timeline_event(
                state,
                TimelineEventType.TASK_CLASSIFIED,
                message=f"Task classified as {task_kind}.",
                payload={"task_kind": task_kind},
            ),
        }


# [Moved to orchestrator/nodes/utils.py]


def _build_task_plan(state: OrchestratorState, complexity_reason: str) -> TaskPlan:
    """Create an ordered, structured decomposition for complex tasks."""
    task_text = state.normalized_task_text or state.task.task_text
    normalized_task_text = " ".join(task_text.split())
    task_text_preview = normalized_task_text[:100] + (
        "..." if len(normalized_task_text) > 100 else ""
    )

    step_one_title = "Inspect Relevant Code Paths"
    step_one_outcome = "Identify the exact files, interfaces, and tests to touch."
    step_two_title = "Implement the Smallest Safe Slice"
    step_two_outcome = (
        "Apply the minimal change set that satisfies the task without widening scope."
    )

    if complexity_reason == "architectural_task":
        step_one_title = "Inspect Architectural Boundaries"
        step_one_outcome = "Identify impacted modules, interfaces, and coupling constraints."
    elif complexity_reason == "ambiguous_task":
        step_one_title = "Investigate Root Cause and Scope"
        step_one_outcome = "Narrow ambiguity into a concrete file-level implementation target."

    elif complexity_reason == "multi_file_task":
        step_two_title = "Sequence Multi-file Changes Safely"
        step_two_outcome = (
            "Apply coherent edits across files while preserving interface consistency."
        )

    return TaskPlan(
        triggered=True,
        complexity_reason=complexity_reason,
        steps=[
            TaskPlanStep(
                step_id="1",
                title=step_one_title,
                expected_outcome=step_one_outcome,
            ),
            TaskPlanStep(
                step_id="2",
                title=step_two_title,
                expected_outcome=step_two_outcome,
            ),
            TaskPlanStep(
                step_id="3",
                title="Verify and Summarize",
                expected_outcome=(
                    "Run focused checks proving "
                    f"'{task_text_preview}' is satisfied and summarize outcomes."
                ),
            ),
        ],
    )


def plan_task(state_input: OrchestratorState) -> dict[str, Any]:
    """Prepare a structural decomposition for the task (T-108)."""
    state = _ensure_state(state_input)
    with start_optional_span(
        tracer_name="orchestrator.graph",
        span_name="orchestrator.node.plan_task",
        attributes={"openinference.span.kind": SPAN_KIND_CHAIN},
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
    ):
        complexity_reason = _task_complexity_reason(state)
        if complexity_reason is None:
            set_span_input_output(input_data=state.task_kind, output_data="plan-not-required")
            return {
                "current_step": "plan_task",
                "task_plan": None,
                "progress_updates": _progress_update(
                    state, "planning skipped: task is straightforward"
                ),
            }

        task_plan = _build_task_plan(state, complexity_reason)
        set_span_input_output(
            input_data={"kind": state.task_kind, "reason": complexity_reason},
            output_data=task_plan.model_dump(mode="json"),
        )
        return {
            "current_step": "plan_task",
            "task_plan": task_plan.model_dump(),
            "progress_updates": _progress_update(
                state, f"structured plan generated ({complexity_reason})"
            ),
            **_timeline_event(
                state,
                TimelineEventType.TASK_PLANNED,
                message=f"Created structured plan (reason: {complexity_reason}).",
                payload={"planning": "generated", **task_plan.model_dump(mode="json")},
            ),
        }
