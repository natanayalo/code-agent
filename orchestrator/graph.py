"""LangGraph workflow skeleton for the orchestrator happy path."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, Literal

from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from db.base import utc_now
from db.enums import TimelineEventType
from orchestrator.constants import (
    COMPLEX_TASK_MARKERS,
    HIGH_QUALITY_REQUEST_MARKERS,
    LOW_COST_REQUEST_MARKERS,
)
from orchestrator.review import REPAIR_REQUEST_CONSTRAINT, review_result
from orchestrator.state import (
    SUPPORTED_WORKER_TYPES,
    ApprovalCheckpoint,
    OrchestratorState,
    RouteDecision,
    SessionStateUpdate,
    TaskPlan,
    TaskPlanStep,
    TaskTimelineEventState,
    VerificationFailureKind,
    VerificationReport,
    VerificationReportItem,
    WorkerDispatch,
    WorkerType,
)
from orchestrator.task_spec import (
    build_task_spec_for_request,
    contains_marker,
    is_destructive_task,
    validate_task_spec_policy,
)
from tools import coerce_permission_level
from tools.numeric import coerce_positive_int_like
from workers import ArtifactReference, Worker, WorkerProfile, WorkerRequest, WorkerResult

logger = logging.getLogger(__name__)

GEMINI_WORKER: Final[WorkerType] = "gemini"
CODEX_WORKER: Final[WorkerType] = "codex"
OPENROUTER_WORKER: Final[WorkerType] = "openrouter"

ORCHESTRATOR_NODE_SEQUENCE = (
    "ingest_task",
    "classify_task",
    "plan_task",
    "generate_task_spec",
    "load_memory",
    "choose_worker",
    "check_approval",
    "await_approval",
    "dispatch_job",
    "await_result",
    "verify_result",
    "review_result",
    "summarize_result",
    "persist_memory",
)

_COMPLEX_TASK_PATTERN = re.compile(
    rf"(?<![\w-])(?:{'|'.join(re.escape(marker) for marker in COMPLEX_TASK_MARKERS)})(?![\w-])"
)

DEFAULT_ORCHESTRATOR_TIMEOUT_SECONDS = 330
ORCHESTRATOR_TIMEOUT_GRACE_SECONDS = 30
_WORKER_FAILURE_REROUTE_KINDS = frozenset(
    {
        "compile",
        "test",
        "tool_runtime",
        "context_window",
        "provider_error",
        "unknown",
    }
)
_VERIFICATION_FAILURE_REROUTE_KINDS = frozenset({"test_regression", "scope_mismatch", "unknown"})
_WORKER_FAILURE_RETRY_SAME_WORKER_KINDS = frozenset(
    {
        "sandbox_infra",
        "provider_auth",
        "permission_denied",
    }
)


def _resolve_orchestrator_timeout_seconds(state: OrchestratorState) -> int:
    """Resolve the outer worker timeout envelope from the task budget."""
    budget = state.task.budget

    explicit_timeout = coerce_positive_int_like(budget.get("orchestrator_timeout_seconds"))
    if explicit_timeout is not None:
        return explicit_timeout

    worker_timeout_seconds = coerce_positive_int_like(budget.get("worker_timeout_seconds"))
    if worker_timeout_seconds is None:
        max_minutes = coerce_positive_int_like(budget.get("max_minutes"))
        if max_minutes is not None:
            worker_timeout_seconds = max_minutes * 60

    if worker_timeout_seconds is not None:
        return worker_timeout_seconds + ORCHESTRATOR_TIMEOUT_GRACE_SECONDS

    return DEFAULT_ORCHESTRATOR_TIMEOUT_SECONDS


def _timed_out_worker_result(timeout_seconds: int) -> WorkerResult:
    """Build a structured timeout result for the outer orchestrator envelope."""
    return WorkerResult(
        status="failure",
        summary=(
            "Worker execution exceeded the orchestrator timeout envelope "
            f"({timeout_seconds}s) and was cancelled."
        ),
        failure_kind="timeout",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="inspect_workspace_artifacts",
    )


def _cancelled_worker_result() -> WorkerResult:
    """Build a structured result for an externally cancelled worker run."""
    return WorkerResult(
        status="failure",
        summary="Worker execution was cancelled before it returned a result.",
        failure_kind="timeout",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="await_manual_follow_up",
    )


def _unexpected_worker_error_result(exc: Exception) -> WorkerResult:
    """Build a structured result for unexpected worker crashes."""
    detail = str(exc).strip()
    summary = (
        f"Worker execution crashed unexpectedly: {type(exc).__name__}: {detail}"
        if detail
        else f"Worker execution crashed unexpectedly: {type(exc).__name__}."
    )
    return WorkerResult(
        status="error",
        summary=summary,
        failure_kind="unknown",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="inspect_worker_configuration",
    )


def _consume_worker_task_result(
    worker_task: asyncio.Task[WorkerResult],
    *,
    worker_type: str,
    session_id: str | None,
) -> None:
    """Drain a background worker task result so cleanup never leaks task exceptions."""
    try:
        worker_task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception(
            "Worker task raised while cancellation cleanup was settling",
            extra={
                "session_id": session_id,
                "worker_type": worker_type,
            },
        )


async def _settle_cancelled_worker_task(
    worker_task: asyncio.Task[WorkerResult],
    *,
    worker_type: str,
    session_id: str | None,
    grace_period_seconds: int = 3,
) -> WorkerResult | None:
    """Cancel the task and optionally wait for it to yield a graceful partial result."""
    worker_task.cancel()

    try:
        return await asyncio.wait_for(asyncio.shield(worker_task), timeout=grace_period_seconds)
    except (TimeoutError, asyncio.CancelledError):
        pass
    except Exception:
        logger.warning(
            "Unexpected exception while waiting for graceful worker cancellation",
            exc_info=True,
            extra={"session_id": session_id, "worker_type": worker_type},
        )
        pass

    if worker_task.done() and not worker_task.cancelled():
        try:
            return worker_task.result()
        except Exception:
            logger.warning(
                "Unexpected exception while extracting worker task result after cancellation",
                exc_info=True,
                extra={"session_id": session_id, "worker_type": worker_type},
            )
            pass

    if not worker_task.done():
        worker_task.add_done_callback(
            lambda task: _consume_worker_task_result(
                task,
                worker_type=worker_type,
                session_id=session_id,
            )
        )
    return None


async def _await_worker_with_timeout(
    worker: Worker,
    request: WorkerRequest,
    *,
    worker_type: str,
    session_id: str | None,
    timeout_seconds: int,
) -> tuple[WorkerResult, str]:
    """Run a worker behind the outer orchestrator timeout/cancel envelope."""

    async def run_worker() -> WorkerResult:
        return await worker.run(request)

    worker_task: asyncio.Task[WorkerResult] = asyncio.create_task(run_worker())
    try:
        result = await asyncio.wait_for(asyncio.shield(worker_task), timeout=timeout_seconds)
    except TimeoutError:
        logger.warning(
            "Worker execution exceeded the orchestrator timeout envelope",
            extra={
                "session_id": session_id,
                "worker_type": worker_type,
                "timeout_seconds": timeout_seconds,
            },
        )
        partial_result = await _settle_cancelled_worker_task(
            worker_task,
            worker_type=worker_type,
            session_id=session_id,
        )
        if partial_result is not None:
            return (
                partial_result,
                (f"worker timed out but yielded partial state after {timeout_seconds}s"),
            )

        return _timed_out_worker_result(
            timeout_seconds
        ), f"worker timed out after {timeout_seconds}s"
    except asyncio.CancelledError:
        logger.warning(
            "Worker execution was cancelled at the orchestrator boundary",
            extra={
                "session_id": session_id,
                "worker_type": worker_type,
            },
        )
        partial_result = await _settle_cancelled_worker_task(
            worker_task,
            worker_type=worker_type,
            session_id=session_id,
        )
        if partial_result is not None:
            return partial_result, "worker execution cancelled but yielded partial state"

        return _cancelled_worker_result(), "worker execution cancelled"
    except Exception as exc:
        logger.exception(
            "Worker execution crashed unexpectedly at the orchestrator boundary",
            extra={
                "session_id": session_id,
                "worker_type": worker_type,
            },
        )
        return _unexpected_worker_error_result(exc), "worker crashed unexpectedly"
    return result, "worker result received"


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
    """Create one or more structured timeline events for state merging.

    Returns a dictionary intended for dictionary spreading (**) into the node response.
    Includes both the list of events and the monotonic count delta.
    """
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
    if any(keyword in normalized_text for keyword in ("investigate", "debug", "analyze")):
        return "ambiguous"
    return "implementation"


def _is_already_approved(state: OrchestratorState) -> bool:
    """Return True if the task has already been approved via API or orchestrator."""
    approval_data = state.task.constraints.get("approval")
    if isinstance(approval_data, Mapping):
        status = str(approval_data.get("status") or "").strip().lower()
        source = str(approval_data.get("source") or "").strip().lower()
        if status == "approved" and source in {"api", "orchestrator"}:
            return True
    return False


def _task_requires_approval(state: OrchestratorState) -> bool:
    """Return True if the task involves destructive actions or requires permission."""
    if _is_already_approved(state):
        return False

    constraints = state.task.constraints
    task_text = state.normalized_task_text or state.task.task_text

    # Check TaskSpec override first (generated by the deterministic spec node)
    if state.task_spec is not None and state.task_spec.requires_permission:
        return True

    if constraints.get("requires_approval") is True:
        return True
    return is_destructive_task(task_text, constraints)


def _build_approval_checkpoint(state: OrchestratorState) -> ApprovalCheckpoint:
    """Build approval metadata for the current task, if required."""
    if not _task_requires_approval(state):
        return ApprovalCheckpoint()

    task_text = state.normalized_task_text or state.task.task_text
    is_destructive = is_destructive_task(task_text, state.task.constraints)

    # Priority: TaskSpec reason > explicit constraint reason > default reason
    reason = state.task.constraints.get("approval_reason")
    if state.task_spec is not None and state.task_spec.permission_reason:
        reason = state.task_spec.permission_reason

    if not isinstance(reason, str) or not reason.strip():
        reason = (
            "Task includes a potentially destructive action."
            if is_destructive
            else "Manual approval required for this task."
        )

    task_identifier = state.task.task_id or "pending"
    return ApprovalCheckpoint(
        required=True,
        status="pending",
        approval_type="destructive_action" if is_destructive else "manual_approval",
        reason=reason,
        resume_token=f"approval-{task_identifier}",
    )


def _route_after_check_approval(state_input: OrchestratorState) -> str:
    """Route either to the approval interrupt or straight to dispatch."""
    state = _ensure_state(state_input)
    return "await_approval" if state.approval.required else "dispatch_job"


def _coerce_approval_decision(resume_value: Any) -> bool:
    """Normalize LangGraph resume payloads into a boolean approval decision."""
    if isinstance(resume_value, bool):
        return resume_value

    if isinstance(resume_value, dict):
        val = resume_value.get("approved")
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "yes", "y", "1", "approve", "approved")
        return False

    if isinstance(resume_value, str):
        return resume_value.lower() in ("true", "yes", "y", "1", "approve", "approved")

    return False


def _route_after_await_approval(state_input: OrchestratorState) -> str:
    """Continue to dispatch only when the destructive action was approved."""
    state = _ensure_state(state_input)
    return "dispatch_job" if state.approval.status == "approved" else "summarize_result"


def _build_worker_request(state: OrchestratorState) -> WorkerRequest:
    """Build the typed worker request from orchestrator state."""
    task_text = state.normalized_task_text or state.task.task_text
    repair_task_text = state.task.constraints.get(REPAIR_REQUEST_CONSTRAINT)
    if isinstance(repair_task_text, str) and repair_task_text.strip():
        task_text = repair_task_text.strip()

    return WorkerRequest(
        session_id=state.session.session_id if state.session is not None else None,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        task_text=task_text,
        memory_context=state.memory.model_dump(),
        task_plan=state.task_plan.model_dump(mode="json") if state.task_plan is not None else None,
        task_spec=state.task_spec.model_dump(mode="json") if state.task_spec is not None else None,
        constraints=dict(state.task.constraints),
        budget=dict(state.task.budget),
        secrets=dict(state.task.secrets),
        tools=state.task.tools,
        worker_profile=state.dispatch.worker_profile or state.route.chosen_profile,
        runtime_mode=state.dispatch.runtime_mode or state.route.runtime_mode,
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


def _configured_workers(
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
) -> dict[str, Worker]:
    """Return the workers that are actually wired into the graph."""
    result: dict[str, Worker] = {CODEX_WORKER: worker or _DefaultFakeWorker()}
    if gemini_worker is not None:
        result[GEMINI_WORKER] = gemini_worker
    if openrouter_worker is not None:
        result[OPENROUTER_WORKER] = openrouter_worker
    return result


def _execution_profile_sort_key(profile: WorkerProfile) -> tuple[int, int, str]:
    """Rank profiles deterministically for execution routing defaults."""
    runtime_rank = (
        0
        if profile.runtime_mode == "native_agent"
        else 1
        if profile.runtime_mode == "tool_loop"
        else 2
    )
    mutation_rank = 0 if profile.mutation_policy == "patch_allowed" else 1
    return (runtime_rank, mutation_rank, profile.name)


def _select_default_profile_for_worker(
    profiles: Mapping[str, WorkerProfile],
    worker_type: WorkerType,
) -> str | None:
    """Pick one execution-capable profile for a worker type."""
    candidates = [
        profile
        for profile in profiles.values()
        if profile.worker_type == worker_type
        and profile.runtime_mode in {"native_agent", "tool_loop"}
        and (not profile.capability_tags or "execution" in profile.capability_tags)
    ]
    if not candidates:
        return None
    candidates.sort(key=_execution_profile_sort_key)
    return candidates[0].name


def _routable_execution_profiles(
    state: OrchestratorState,
    profiles: Mapping[str, WorkerProfile],
    available_workers: frozenset[str],
) -> dict[str, WorkerProfile]:
    """Filter configured profiles to those compatible with the task request."""
    SUPPORTED_RUNTIME_MODES: Final = {"native_agent", "tool_loop"}
    delivery_mode = state.task_spec.delivery_mode if state.task_spec is not None else None
    requires_read_only = bool(state.task.constraints.get("read_only"))

    selected: dict[str, WorkerProfile] = {}
    for name, profile in profiles.items():
        if profile.worker_type not in available_workers:
            continue
        if profile.runtime_mode not in SUPPORTED_RUNTIME_MODES:
            continue
        if profile.capability_tags and "execution" not in profile.capability_tags:
            continue

        # Strict mutation policy matching:
        # 1. If task is read-only, only read-only profiles are allowed.
        # 2. If task allows mutations, only patch-allowed profiles are allowed.
        if requires_read_only and profile.mutation_policy != "read_only":
            continue
        if not requires_read_only and profile.mutation_policy == "read_only":
            continue

        if delivery_mode and profile.supported_delivery_modes:
            if delivery_mode not in profile.supported_delivery_modes:
                continue
        selected[name] = profile
    return selected


def _route_for_profile(
    profile: WorkerProfile,
    *,
    reason: str,
    override_applied: bool,
) -> RouteDecision:
    """Build a route decision pinned to a concrete worker profile."""
    return RouteDecision(
        chosen_worker=profile.worker_type,
        chosen_profile=profile.name,
        runtime_mode=profile.runtime_mode,
        route_reason=reason,
        override_applied=override_applied,
    )


def _route_from_worker_choice(
    worker_route: RouteDecision,
    profiles: Mapping[str, WorkerProfile],
) -> RouteDecision:
    """Convert a worker-only routing decision into a profile-aware decision."""
    chosen_worker = worker_route.chosen_worker
    if chosen_worker is None:
        return worker_route

    profile_name = _select_default_profile_for_worker(profiles, chosen_worker)
    if profile_name is None:
        return worker_route

    profile = profiles[profile_name]
    return RouteDecision(
        chosen_worker=chosen_worker,
        chosen_profile=profile_name,
        runtime_mode=profile.runtime_mode,
        route_reason=worker_route.route_reason,
        override_applied=worker_route.override_applied,
    )


def _unconfigured_worker_result(
    worker_type: str | None,
    *,
    configured_workers: frozenset[str],
) -> WorkerResult:
    """Return a structured error when routing selects an unavailable worker."""
    configured_workers_text = ", ".join(sorted(configured_workers))
    selected_worker = worker_type or "unknown"
    return WorkerResult(
        status="error",
        summary=(
            f"No worker is configured for route '{selected_worker}'. "
            f"Configured workers: {configured_workers_text}."
        ),
        failure_kind="provider_error",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="configure_requested_worker",
    )


def _unconfigured_worker_profile_result(
    profile_name: str,
    *,
    configured_profiles: frozenset[str],
) -> WorkerResult:
    """Return a structured error when routing selects an unavailable worker profile."""
    configured_profiles_text = ", ".join(sorted(configured_profiles)) or "none"
    return WorkerResult(
        status="error",
        summary=(
            f"No routable worker profile is available for route '{profile_name}'. "
            f"Configured profiles: {configured_profiles_text}."
        ),
        failure_kind="provider_error",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="configure_requested_worker_profile",
    )


def _worker_route_missing_profile_result(
    worker_type: str | None,
    *,
    configured_profiles: frozenset[str],
) -> WorkerResult:
    """Return a structured error when worker selection has no routable execution profile."""
    configured_profiles_text = ", ".join(sorted(configured_profiles)) or "none"
    selected_worker = worker_type or "unknown"
    return WorkerResult(
        status="error",
        summary=(
            f"No routable worker profile is available for worker route '{selected_worker}'. "
            f"Configured profiles: {configured_profiles_text}."
        ),
        failure_kind="provider_error",
        commands_run=[],
        files_changed=[],
        test_results=[],
        artifacts=[],
        next_action_hint="configure_requested_worker_profile",
    )


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
    task_text = state.normalized_task_text or state.task.task_text
    task_kind = _classify_task_kind(task_text)
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


def _build_task_plan(state: OrchestratorState, complexity_reason: str) -> TaskPlan:
    """Create an ordered, structured decomposition for complex tasks."""
    task_text = state.normalized_task_text or state.task.task_text
    normalized_task_text = " ".join(task_text.split())
    task_text_preview = normalized_task_text[:100] + (
        "..." if len(normalized_task_text) > 100 else ""
    )
    # TODO(T-108 follow-up): replace this static scaffold with dynamic task-specific
    # decomposition once planner heuristics (or a planner model call) are introduced.
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
    """Generate a structured plan only for tasks classified as complex."""
    state = _ensure_state(state_input)
    complexity_reason = _task_complexity_reason(state)
    if complexity_reason is None:
        return {
            "current_step": "plan_task",
            "task_plan": None,
            "progress_updates": _progress_update(
                state, "planning skipped: task is straightforward"
            ),
            **_timeline_event(
                state,
                TimelineEventType.TASK_PLANNED,
                message="Planning skipped for straightforward task.",
                payload={"planning": "skipped"},
            ),
        }

    task_plan = _build_task_plan(state, complexity_reason)
    return {
        "current_step": "plan_task",
        "task_plan": task_plan.model_dump(),
        "progress_updates": _progress_update(
            state,
            f"structured plan generated ({complexity_reason})",
        ),
        **_timeline_event(
            state,
            TimelineEventType.TASK_PLANNED,
            message="Structured plan generated for complex task.",
            payload={"planning": "generated", "complexity_reason": complexity_reason},
        ),
    }


def generate_task_spec(state_input: OrchestratorState) -> dict[str, Any]:
    """Generate the structured task contract before memory loading and worker routing."""
    state = _ensure_state(state_input)
    task_spec = build_task_spec_for_request(
        state.task,
        task_kind=state.task_kind,
        task_plan=state.task_plan,
    )
    policy_violations = validate_task_spec_policy(task_spec)
    progress_message = "task spec generated"
    if policy_violations:
        progress_message = "task spec generated with policy warnings"

    response: dict[str, Any] = {
        "current_step": "generate_task_spec",
        "task_spec": task_spec.model_dump(),
        "progress_updates": _progress_update(state, progress_message),
        **_timeline_event(
            state,
            TimelineEventType.TASK_SPEC_GENERATED,
            message="TaskSpec generated for worker routing.",
            payload={
                "task_spec": task_spec.model_dump(mode="json"),
                "policy_violations": policy_violations,
            },
        ),
    }
    if policy_violations:
        response["errors"] = [
            *state.errors,
            *(f"task_spec_policy:{violation}" for violation in policy_violations),
        ]
        response["result"] = WorkerResult(
            status="error",
            summary=(
                "Task generation halted due to safety policy violations: "
                f"{', '.join(policy_violations)}"
            ),
            failure_kind="unknown",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="halt_policy_violation",
        )
    return response


def _route_after_generate_task_spec(state_input: OrchestratorState) -> str:
    """Route either to load_memory or summarize_result if policy violations occur."""
    state = _ensure_state(state_input)
    policy_errors = [e for e in state.errors if e.startswith("task_spec_policy:")]
    return "summarize_result" if policy_errors else "load_memory"


def load_memory(state_input: OrchestratorState) -> dict[str, Any]:
    """Preserve the current memory context for the skeleton graph."""
    state = _ensure_state(state_input)
    return {
        "current_step": "load_memory",
        "memory": state.memory.model_dump(),
        "progress_updates": _progress_update(state, "memory context loaded"),
        **_timeline_event(
            state,
            TimelineEventType.MEMORY_LOADED,
            message=(
                f"Loaded {len(state.memory.personal)} personal and "
                f"{len(state.memory.project)} project memory entries."
            ),
        ),
    }


def _route_by_preference(
    preferred: WorkerType,
    fallbacks: tuple[WorkerType, ...],
    reason: str,
    available_workers: frozenset[str],
) -> RouteDecision:
    """Pick the preferred worker when available, or the first available fallback.

    - preferred available  → reason (e.g. 'high_stakes_refactor')
    - fallback available   → 'preferred_unavailable'  (task runs on the fallback)
    - neither available    → 'runtime_unavailable'    (dispatch will fail explicitly)
    """
    if preferred in available_workers:
        return RouteDecision(
            chosen_worker=preferred,
            route_reason=reason,
            override_applied=False,
        )
    for fallback in fallbacks:
        if fallback in available_workers:
            return RouteDecision(
                chosen_worker=fallback,
                route_reason="preferred_unavailable",
                override_applied=False,
            )
    # Neither available - keep the preferred intent; dispatch will fail explicitly.
    return RouteDecision(
        chosen_worker=preferred,
        route_reason="runtime_unavailable",
        override_applied=False,
    )


def _compute_legacy_route_decision(
    state: OrchestratorState,
    available_workers: frozenset[str],
) -> RouteDecision:
    """Apply T-071 routing heuristics and T-072 manual override in priority order."""

    # T-072: manual override — honor when the requested runtime is available;
    # fail explicitly otherwise so state never silently claims a worker that isn't present.
    if state.task.worker_override is not None:
        worker_override = state.task.worker_override
        if worker_override in available_workers:
            return RouteDecision(
                chosen_worker=worker_override,
                route_reason="manual_override",
                override_applied=True,
            )
        logger.warning(
            "Manual override requested unavailable worker; routing will fail at dispatch",
            extra={"worker": worker_override, "available": sorted(available_workers)},
        )
        return RouteDecision(
            chosen_worker=worker_override,
            route_reason="runtime_unavailable",
            override_applied=True,
        )

    # T-071: heuristic 1 — escalate to an alternate worker after prior failure.
    if state.attempt_count > 0 and state.dispatch.worker_type is not None:
        prior_worker: WorkerType = state.dispatch.worker_type
        escalation_reason: str | None = None
        if state.verification is not None and state.verification.status == "failed":
            verification_failure_kind = state.verification.failure_kind or "unknown"
            escalation_reason = (
                "verifier_failed_previous_run"
                if verification_failure_kind in _VERIFICATION_FAILURE_REROUTE_KINDS
                else None
            )
        if (
            escalation_reason is None
            and state.result is not None
            and state.result.status != "success"
        ):
            failure_kind = state.result.failure_kind or "unknown"
            escalation_reason = (
                "previous_worker_failed" if failure_kind in _WORKER_FAILURE_REROUTE_KINDS else None
            )

        if escalation_reason is not None:
            alternates = tuple(
                worker for worker in SUPPORTED_WORKER_TYPES if worker != prior_worker
            )

            for alternate in alternates:
                if alternate not in available_workers:
                    continue
                logger.info(
                    "Routing to alternate worker due to prior failure",
                    extra={
                        "prior_worker": prior_worker,
                        "alternate_worker": alternate,
                        "reason": escalation_reason,
                    },
                )
                return RouteDecision(
                    chosen_worker=alternate,
                    route_reason=escalation_reason,
                    override_applied=False,
                )
            desired_alternate = alternates[0]
            # Alternate unavailable — fail explicitly rather than blind retry of the failed worker.
            logger.warning(
                "Escalation requires alternate worker but it is unavailable; failing explicitly",
                extra={"prior_worker": prior_worker, "alternate_worker": desired_alternate},
            )
            return RouteDecision(
                chosen_worker=desired_alternate,
                route_reason="runtime_unavailable",
                override_applied=False,
            )

        # Environment/auth/permission failures should retry with the same worker after fixes.
        if state.result is not None and state.result.status != "success":
            failure_kind = state.result.failure_kind or "unknown"
            if failure_kind in _WORKER_FAILURE_RETRY_SAME_WORKER_KINDS:
                if prior_worker in available_workers:
                    return RouteDecision(
                        chosen_worker=prior_worker,
                        route_reason="environment_retry_same_worker",
                        override_applied=False,
                    )
                logger.warning(
                    "Environment retry requires same worker but it is unavailable",
                    extra={"prior_worker": prior_worker},
                )
                return RouteDecision(
                    chosen_worker=prior_worker,
                    route_reason="runtime_unavailable",
                    override_applied=False,
                )

    # T-071: heuristic 2 — explicit budget preference.
    budget = state.task.budget
    task_text = state.normalized_task_text or state.task.task_text
    constraints = state.task.constraints
    if (
        budget.get("prefer_high_quality")
        or constraints.get("prefer_high_quality")
        or contains_marker(task_text, HIGH_QUALITY_REQUEST_MARKERS)
    ):
        return _route_by_preference(
            GEMINI_WORKER,
            (OPENROUTER_WORKER, CODEX_WORKER),
            "budget_preference",
            available_workers,
        )
    if (
        budget.get("prefer_low_cost")
        or constraints.get("prefer_low_cost")
        or contains_marker(task_text, LOW_COST_REQUEST_MARKERS)
    ):
        return _route_by_preference(
            CODEX_WORKER,
            (OPENROUTER_WORKER, GEMINI_WORKER),
            "budget_preference",
            available_workers,
        )

    # T-071: heuristic 3 — task shape.
    task_kind = state.task_kind
    if task_kind == "architecture":
        return _route_by_preference(
            GEMINI_WORKER,
            (OPENROUTER_WORKER, CODEX_WORKER),
            "high_stakes_refactor",
            available_workers,
        )
    if task_kind == "ambiguous":
        return _route_by_preference(
            GEMINI_WORKER,
            (OPENROUTER_WORKER, CODEX_WORKER),
            "ambiguous_task",
            available_workers,
        )
    if _task_complexity_reason(state) == "multi_file_task":
        return _route_by_preference(
            GEMINI_WORKER,
            (OPENROUTER_WORKER, CODEX_WORKER),
            "high_stakes_refactor",
            available_workers,
        )
    return _route_by_preference(
        CODEX_WORKER,
        (OPENROUTER_WORKER, GEMINI_WORKER),
        "cheap_mechanical_change",
        available_workers,
    )


def _compute_profile_route_decision(
    state: OrchestratorState,
    available_workers: frozenset[str],
    available_profiles: Mapping[str, WorkerProfile],
) -> RouteDecision:
    """Compute routing through configured worker profiles."""
    routable_profiles = _routable_execution_profiles(state, available_profiles, available_workers)

    profile_override = state.task.worker_profile_override
    if isinstance(profile_override, str) and profile_override.strip():
        requested_profile = profile_override.strip()
        profile = routable_profiles.get(requested_profile)
        if profile is not None:
            return _route_for_profile(
                profile,
                reason="manual_profile_override",
                override_applied=True,
            )

        # Intent was a specific profile, but it is not routable.
        # Capture the worker type from available profiles if possible for better error reporting.
        chosen_worker: WorkerType | None = None
        known_profile = available_profiles.get(requested_profile)
        if known_profile is not None:
            chosen_worker = known_profile.worker_type
        else:
            # Fallback to legacy selection among routable workers if profile is totally unknown
            profiled_workers = frozenset({p.worker_type for p in routable_profiles.values()})
            if profiled_workers:
                fallback = _compute_legacy_route_decision(state, profiled_workers)
                chosen_worker = fallback.chosen_worker
            else:
                chosen_worker = None

        return RouteDecision(
            chosen_worker=chosen_worker,
            chosen_profile=requested_profile,
            runtime_mode=None,
            route_reason="runtime_unavailable",
            override_applied=True,
        )

    worker_override = state.task.worker_override
    if worker_override is not None:
        profile_name = _select_default_profile_for_worker(routable_profiles, worker_override)
        if profile_name is not None:
            return _route_for_profile(
                routable_profiles[profile_name],
                reason="manual_override",
                override_applied=True,
            )
        return RouteDecision(
            chosen_worker=worker_override,
            route_reason="runtime_unavailable",
            override_applied=True,
        )

    profiled_workers = frozenset({p.worker_type for p in routable_profiles.values()})
    worker_route = _compute_legacy_route_decision(state, profiled_workers)
    return _route_from_worker_choice(worker_route, routable_profiles)


def _compute_route_decision(
    state: OrchestratorState,
    available_workers: frozenset[str],
    *,
    available_profiles: Mapping[str, WorkerProfile] | None = None,
) -> RouteDecision:
    """Compute routing with profile-aware selection when profiles are configured."""
    if not available_profiles:
        return _compute_legacy_route_decision(state, available_workers)
    return _compute_profile_route_decision(state, available_workers, available_profiles)


def build_choose_worker_node(
    available_workers: frozenset[str],
    *,
    available_profiles: Mapping[str, WorkerProfile] | None = None,
) -> Callable[[OrchestratorState], dict[str, Any]]:
    """Create the choose-worker node bound to the given set of available workers."""

    def choose_worker_node(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)
        route = _compute_route_decision(
            state,
            available_workers,
            available_profiles=available_profiles,
        )
        return {
            "current_step": "choose_worker",
            "route": route.model_dump(),
            "progress_updates": _progress_update(
                state,
                f"worker selected: {route.chosen_worker} (reason: {route.route_reason})",
            ),
            **_timeline_event(
                state,
                TimelineEventType.WORKER_SELECTED,
                message=f"Worker selected: {route.chosen_worker}",
                payload=route.model_dump(),
            ),
        }

    return choose_worker_node


def choose_worker(state_input: OrchestratorState) -> dict[str, Any]:
    """Apply routing heuristics; treats all known workers as available.

    Use build_choose_worker_node() when the graph knows which workers are wired in.
    """
    state = _ensure_state(state_input)
    route = _compute_route_decision(state, frozenset(SUPPORTED_WORKER_TYPES))
    return {
        "current_step": "choose_worker",
        "route": route.model_dump(),
        "progress_updates": _progress_update(
            state,
            f"worker selected: {route.chosen_worker} (reason: {route.route_reason})",
        ),
    }


def check_approval(state_input: OrchestratorState) -> dict[str, Any]:
    """Persist approval metadata before any destructive action is dispatched."""
    state = _ensure_state(state_input)
    approval = _build_approval_checkpoint(state)
    progress_message = "approval requested" if approval.required else "approval not required"
    return {
        "current_step": "check_approval",
        "approval": approval.model_dump(),
        "progress_updates": _progress_update(state, progress_message),
        **_timeline_event(
            state,
            TimelineEventType.APPROVAL_REQUESTED,
            message=f"Approval requested: {approval.reason}"
            if approval.required
            else "Approval not required.",
            payload=approval.model_dump() if approval.required else None,
        ),
    }


def await_approval(state_input: OrchestratorState) -> dict[str, Any]:
    """Pause the graph until a destructive action is approved or rejected."""
    state = _ensure_state(state_input)
    approval = state.approval
    if not approval.required:
        return {
            "current_step": "await_approval",
            "approval": approval.model_dump(),
        }

    # If we are resuming after a crash or a new attempt, the approval might already
    # be in the constraints (set by the API decision endpoint).
    if _is_already_approved(state):
        updated_approval = approval.model_copy(update={"status": "approved"})
        return {
            "current_step": "await_approval",
            "approval": updated_approval.model_dump(),
            "progress_updates": _progress_update(state, "approval granted (resumed from state)"),
        }

    task_text = state.normalized_task_text or state.task.task_text
    approved = _coerce_approval_decision(
        interrupt(
            {
                "approval_type": approval.approval_type,
                "reason": approval.reason,
                "resume_token": approval.resume_token,
                "task_text": task_text,
                "chosen_worker": state.route.chosen_worker,
            }
        )
    )

    updated_approval = approval.model_copy(
        update={"status": "approved" if approved else "rejected"},
    )
    progress_message = "approval granted" if approved else "approval rejected"
    response: dict[str, Any] = {
        "current_step": "await_approval",
        "approval": updated_approval.model_dump(),
        "progress_updates": _progress_update(state, progress_message),
    }
    if not approved:
        response["result"] = WorkerResult(
            status="failure",
            summary="Task halted because the requested destructive action was not approved.",
            failure_kind="permission_denied",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="await_manual_follow_up",
        ).model_dump()
        response.update(
            _timeline_event(
                state,
                TimelineEventType.APPROVAL_REJECTED,
                message="Task expansion rejected.",
            )
        )
    else:
        response.update(
            _timeline_event(
                state,
                TimelineEventType.APPROVAL_GRANTED,
                message="Task expansion approved.",
            )
        )
    return response


def dispatch_job(state_input: OrchestratorState) -> dict[str, Any]:
    """Record the chosen worker before awaiting execution."""
    state = _ensure_state(state_input)
    worker_type = state.route.chosen_worker
    assert worker_type is not None, "choose_worker must set route.chosen_worker before dispatch."
    dispatch = WorkerDispatch(
        worker_type=worker_type,
        worker_profile=state.route.chosen_profile,
        runtime_mode=state.route.runtime_mode,
    )
    return {
        "current_step": "dispatch_job",
        "dispatch": dispatch.model_dump(),
        "repair_handoff_requested": False,
        "progress_updates": _progress_update(state, "worker dispatched"),
        **_timeline_event(
            state,
            TimelineEventType.WORKER_DISPATCHED,
            message=f"Dispatched attempt {state.attempt_count} to {worker_type}.",
            payload={"attempt_count": state.attempt_count, "worker_type": worker_type},
        ),
    }


def build_await_result_node(
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
    *,
    configured_profile_names: frozenset[str] = frozenset(),
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """Create the await-result node around the workers wired into the graph."""
    configured_workers = _configured_workers(worker, gemini_worker, openrouter_worker)

    async def await_result(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)
        worker_type = state.dispatch.worker_type or state.route.chosen_worker
        requested_profile = state.dispatch.worker_profile or state.route.chosen_profile
        if state.route.route_reason == "runtime_unavailable":
            if configured_profile_names:
                if requested_profile is None:
                    result = _worker_route_missing_profile_result(
                        worker_type,
                        configured_profiles=configured_profile_names,
                    )
                    progress_message = (
                        f"no routable profile available for worker: {worker_type or 'unknown'}"
                    )
                else:
                    result = _unconfigured_worker_profile_result(
                        requested_profile,
                        configured_profiles=configured_profile_names,
                    )
                    progress_message = (
                        f"worker profile unavailable or incompatible: {requested_profile}"
                    )
            else:
                result = _unconfigured_worker_result(
                    worker_type,
                    configured_workers=frozenset(configured_workers.keys()),
                )
                progress_message = f"worker unavailable: {worker_type or 'unknown'}"
            progress_updates = _progress_update(state, progress_message)
        else:
            bound_worker = configured_workers.get(worker_type or "")
            if bound_worker is None:
                result = _unconfigured_worker_result(
                    worker_type,
                    configured_workers=frozenset(configured_workers.keys()),
                )
                progress_message = f"worker unavailable: {worker_type or 'unknown'}"
                progress_updates = _progress_update(state, progress_message)
            else:
                request = _build_worker_request(state)
                result, progress_message = await _await_worker_with_timeout(
                    bound_worker,
                    request,
                    worker_type=worker_type or "unknown",
                    session_id=request.session_id,
                    timeout_seconds=_resolve_orchestrator_timeout_seconds(state),
                )
                progress_updates = _progress_update(state, progress_message)
        return {
            "current_step": "await_result",
            "result": result.model_dump(),
            "progress_updates": progress_updates,
            **_timeline_event(
                state,
                (
                    TimelineEventType.WORKER_COMPLETED
                    if result.status == "success"
                    else TimelineEventType.WORKER_FAILED
                    if result.status == "failure"
                    else TimelineEventType.WORKER_ERROR
                ),
                message=result.summary or progress_message,
                payload={"status": result.status},
            ),
        }

    return await_result


def _route_after_await_result(state_input: OrchestratorState) -> str:
    """Route from await_result either to verify_result or await_permission_escalation."""
    state = _ensure_state(state_input)
    if state.result is not None and state.result.next_action_hint == "request_higher_permission":
        return "await_permission_escalation"
    return "verify_result"


def await_permission_escalation(state_input: OrchestratorState) -> dict[str, Any]:
    """Pause the graph to request higher tool permissions from the caller."""
    state = _ensure_state(state_input)
    if not state.result or state.result.next_action_hint != "request_higher_permission":
        return {"current_step": "await_permission_escalation"}

    task_text = state.normalized_task_text or state.task.task_text
    requested_permission = state.result.requested_permission
    if not requested_permission:
        logger.error(
            "Worker requested higher permission but 'requested_permission' is missing.",
            extra={"session_id": state.session.session_id if state.session else None},
        )
        failed_result = state.result.model_copy(
            update={
                "status": "error",
                "summary": "Worker requested higher permission but did not specify which one.",
                "next_action_hint": "inspect_worker_configuration",
            }
        )
        return {
            "current_step": "await_permission_escalation",
            "result": failed_result.model_dump(),
            "progress_updates": _progress_update(
                state, "permission request failed: missing permission name"
            ),
            **_timeline_event(
                state,
                TimelineEventType.WORKER_ERROR,
                message="Worker requested higher permission but did not specify which one.",
            ),
        }
    requested_permission_level = coerce_permission_level(requested_permission)
    if requested_permission_level is None:
        logger.error(
            "Worker requested an unknown permission level.",
            extra={
                "session_id": state.session.session_id if state.session else None,
                "requested_permission": requested_permission,
            },
        )
        failed_result = state.result.model_copy(
            update={
                "status": "error",
                "summary": (
                    f"Worker requested an unknown permission level '{requested_permission}'."
                ),
                "requested_permission": None,
                "next_action_hint": "inspect_worker_configuration",
            }
        )
        return {
            "current_step": "await_permission_escalation",
            "result": failed_result.model_dump(),
            "progress_updates": _progress_update(
                state,
                f"permission request failed: invalid permission '{requested_permission}'",
            ),
            **_timeline_event(
                state,
                TimelineEventType.WORKER_ERROR,
                message=f"Worker requested an unknown permission level '{requested_permission}'.",
                payload={"requested_permission": requested_permission},
            ),
        }

    requested_permission_name = requested_permission_level.value
    reason = (
        state.result.summary or f"Worker requested higher permission: {requested_permission_name}"
    )

    approved = _coerce_approval_decision(
        interrupt(
            {
                "approval_type": "permission_escalation",
                "reason": reason,
                "resume_token": f"permission-{state.task.task_id or 'pending'}",
                "task_text": task_text,
                "chosen_worker": state.route.chosen_worker,
                "requested_permission": requested_permission_name,
            }
        )
    )

    if approved:
        new_constraints = dict(state.task.constraints)
        new_constraints["granted_permission"] = requested_permission_name
        updated_task = state.task.model_copy(update={"constraints": new_constraints})

        return {
            "current_step": "await_permission_escalation",
            "task": updated_task.model_dump(),
            "result": None,
            "progress_updates": _progress_update(
                state, f"permission '{requested_permission_name}' granted"
            ),
            **_timeline_event(
                state,
                TimelineEventType.APPROVAL_GRANTED,
                message=f"Permission '{requested_permission_name}' granted.",
                payload={"granted_permission": requested_permission_name},
            ),
        }
    else:
        failed_result = state.result.model_copy(
            update={
                "summary": (
                    "Permission escalation to "
                    f"'{requested_permission_name}' was rejected. Run halted."
                ),
                "failure_kind": "permission_denied",
                "next_action_hint": "await_manual_follow_up",
            }
        )
        return {
            "current_step": "await_permission_escalation",
            "result": failed_result.model_dump(),
            "progress_updates": _progress_update(
                state, f"permission '{requested_permission_name}' rejected"
            ),
            **_timeline_event(
                state,
                TimelineEventType.APPROVAL_REJECTED,
                message=f"Permission '{requested_permission_name}' rejected.",
                payload={"requested_permission": requested_permission_name},
            ),
        }


def _route_after_await_permission_escalation(state_input: OrchestratorState) -> str:
    """Route back to dispatch if approved, else verify failure through verification."""
    state = _ensure_state(state_input)
    if state.result is None:
        return "dispatch_job"
    return "verify_result"


def _route_after_review_result(state_input: OrchestratorState) -> str:
    """Route to a bounded repair handoff when independent review requested it."""
    state = _ensure_state(state_input)
    if state.repair_handoff_requested:
        return "dispatch_job"
    return "summarize_result"


def verify_result(state_input: OrchestratorState) -> dict[str, Any]:
    """Perform deterministic checks on the worker output before summarization."""
    state = _ensure_state(state_input)
    if state.result is None:
        return {
            "current_step": "verify_result",
            "progress_updates": _progress_update(state, "verification skipped: no result"),
        }

    items: list[VerificationReportItem] = []

    # 1. Worker Status
    items.append(
        VerificationReportItem(
            label="worker_status",
            status="passed" if state.result.status == "success" else "failed",
            message=f"Worker reported status: {state.result.status}",
        )
    )

    # 2. Test Results
    failed_tests = [t for t in state.result.test_results if t.status in ("failed", "error")]
    status: Literal["passed", "failed", "warning"] = "warning"
    if state.result.test_results:
        status = "failed" if failed_tests else "passed"
        msg = f"{len(failed_tests)} failed" if failed_tests else "All tests passed"
    else:
        status = "warning"
        msg = "No test results reported"
    items.append(
        VerificationReportItem(
            label="test_results",
            status=status,
            message=msg,
        )
    )

    # 3. File Changes
    if state.result.status == "success" and not state.result.files_changed:
        items.append(
            VerificationReportItem(
                label="file_changes",
                status="warning",
                message="Worker reported success but no files were changed.",
            )
        )
    elif state.result.status != "success" and state.result.files_changed:
        items.append(
            VerificationReportItem(
                label="file_changes",
                status="warning",
                message=(
                    f"Worker reported {state.result.status} "
                    f"but changed {len(state.result.files_changed)} files."
                ),
            )
        )
    else:
        items.append(
            VerificationReportItem(
                label="file_changes",
                status="passed",
                message=f"{len(state.result.files_changed)} files changed.",
            )
        )

    # 4. Command Audit
    failed_commands = [c for c in state.result.commands_run if c.exit_code != 0]
    if failed_commands:
        items.append(
            VerificationReportItem(
                label="command_audit",
                status="warning",
                message=f"{len(failed_commands)} commands exited with non-zero status.",
            )
        )
    else:
        items.append(
            VerificationReportItem(
                label="command_audit",
                status="passed",
                message=f"All {len(state.result.commands_run)} commands exited successfully.",
            )
        )

    # 5. Post-run lint/format
    post_run_lint_format: dict[str, Any] = {}
    if isinstance(state.result.budget_usage, dict):
        lint_metadata = state.result.budget_usage.get("post_run_lint_format")
        if isinstance(lint_metadata, dict):
            post_run_lint_format = lint_metadata
    if post_run_lint_format.get("ran") is False:
        items.append(
            VerificationReportItem(
                label="post_run_lint_format",
                status="passed",
                message="Post-run lint/format step skipped: no detectable command.",
            )
        )
    else:
        lint_errors = post_run_lint_format.get("errors")
        if isinstance(lint_errors, list) and lint_errors:
            items.append(
                VerificationReportItem(
                    label="post_run_lint_format",
                    status="warning",
                    message=f"Post-run lint/format reported {len(lint_errors)} issue(s).",
                )
            )
        elif post_run_lint_format:
            items.append(
                VerificationReportItem(
                    label="post_run_lint_format",
                    status="passed",
                    message="Post-run lint/format completed without reported issues.",
                )
            )

    # Calculate overall status
    report_status: Literal["passed", "failed", "warning"]
    if any(i.status == "failed" for i in items):
        report_status = "failed"
    elif any(i.status == "warning" for i in items):
        report_status = "warning"
    else:
        report_status = "passed"

    report_failure_kind: VerificationFailureKind | None = None
    if report_status == "failed":
        failed_labels = {item.label for item in items if item.status == "failed"}
        if "test_results" in failed_labels:
            report_failure_kind = "test_regression"
        elif "file_changes" in failed_labels:
            report_failure_kind = "scope_mismatch"
        elif "command_audit" in failed_labels:
            report_failure_kind = "risky_command"
        elif "worker_status" in failed_labels:
            report_failure_kind = "worker_failure"
        else:
            report_failure_kind = "unknown"

    report = VerificationReport(
        status=report_status,
        summary=f"Verification {report_status}: {len(items)} checks run.",
        failure_kind=report_failure_kind,
        items=items,
    )

    return {
        "current_step": "verify_result",
        "verification": report.model_dump(),
        "progress_updates": _progress_update(state, f"verification {report_status}"),
        **_timeline_events(
            state,
            (TimelineEventType.VERIFICATION_STARTED, None, None),
            (
                TimelineEventType.VERIFICATION_COMPLETED,
                report.summary,
                report.model_dump(),
            ),
        ),
    }


def summarize_result(state_input: OrchestratorState) -> dict[str, Any]:
    """Ensure the worker result has a human-readable summary."""
    state = _ensure_state(state_input)
    if state.result is None:
        result = WorkerResult(
            status="error",
            summary="Worker did not return a result.",
            failure_kind="unknown",
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
        )
    elif state.result.summary is None:
        worker_name = state.dispatch.worker_type
        assert worker_name is not None, "dispatch_job must set dispatch.worker_type before summary."
        result = state.result.model_copy(
            update={"summary": f"{worker_name} finished with status {state.result.status}"},
        )
    else:
        result = state.result

    # T-117: Append independent reviewer findings to the final summary
    if state.review is not None and state.review.outcome == "findings":
        current_summary = result.summary or ""
        review_lines = [
            "---",
            "### Reviewer Findings",
            state.review.summary,
            "",
        ]
        for finding in state.review.findings:
            review_lines.append(
                f"- **{finding.severity.upper()}**: {finding.title} ({finding.file_path})"
            )
            review_lines.append(f"  {finding.why_it_matters}")

        summary_prefix = f"{current_summary}\n" if current_summary else ""
        result = result.model_copy(
            update={"summary": summary_prefix + "\n".join(review_lines)},
        )

    if state.task_plan is not None and state.task_plan.triggered:
        plan_json = state.task_plan.model_dump_json()
        plan_payload = base64.b64encode(plan_json.encode("utf-8")).decode("utf-8")
        result = result.model_copy(
            update={
                "artifacts": [
                    *result.artifacts,
                    ArtifactReference(
                        name="task_plan",
                        uri=f"data:application/json;base64,{plan_payload}",
                        artifact_type="result_summary",
                    ),
                ]
            }
        )

    # Extract session state update (T-062)
    session_state_update = SessionStateUpdate(
        active_goal=state.normalized_task_text or state.task.task_text,
        files_touched=result.files_changed,
        # TODO: extract decisions_made and identified_risks from result.summary or a dedicated field
    )

    return {
        "current_step": "summarize_result",
        "result": result.model_dump(),
        "session_state_update": session_state_update.model_dump(),
        "progress_updates": _progress_update(state, "result summarized and session state updated"),
        **_timeline_event(
            state,
            TimelineEventType.TASK_COMPLETED
            if result.status == "success"
            else TimelineEventType.TASK_FAILED,
            message=result.summary,
            payload={"status": result.status},
        ),
    }


def persist_memory(state_input: OrchestratorState) -> dict[str, Any]:
    """Terminate the happy path without yet writing memory anywhere."""
    state = _ensure_state(state_input)
    # Placeholder for skeptical memory update
    return {
        "current_step": "persist_memory",
        "memory_to_persist": [entry.model_dump() for entry in state.memory_to_persist],
        "progress_updates": _progress_update(state, "memory persistence queued"),
    }


def build_review_result_node(
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """Create the review-result node around the workers wired into the graph."""
    configured_workers = _configured_workers(worker, gemini_worker, openrouter_worker)

    async def review_result_node(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)
        return await review_result(state, worker_factory=configured_workers)

    return review_result_node


def build_orchestrator_graph(
    *,
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
    worker_profiles: Mapping[str, WorkerProfile] | None = None,
    enable_worker_profiles: bool = False,
    checkpointer: BaseCheckpointSaver | None = None,
    interrupt_before: Literal["*"] | list[str] | None = None,
    interrupt_after: Literal["*"] | list[str] | None = None,
) -> Any:
    """Build and compile the linear LangGraph happy-path skeleton."""
    builder = StateGraph(OrchestratorState)
    builder.add_node("ingest_task", RunnableLambda(ingest_task))
    builder.add_node("classify_task", RunnableLambda(classify_task))
    builder.add_node("plan_task", RunnableLambda(plan_task))
    builder.add_node("generate_task_spec", RunnableLambda(generate_task_spec))
    builder.add_node("load_memory", RunnableLambda(load_memory))
    available_workers: frozenset[str] = frozenset(
        _configured_workers(worker, gemini_worker, openrouter_worker).keys()
    )
    configured_profiles = dict(worker_profiles or {})
    active_profiles = configured_profiles if enable_worker_profiles else None
    profile_names = frozenset(configured_profiles.keys()) if enable_worker_profiles else frozenset()
    builder.add_node(
        "choose_worker",
        RunnableLambda(
            build_choose_worker_node(
                available_workers,
                available_profiles=active_profiles,
            )
        ),
    )
    builder.add_node("check_approval", RunnableLambda(check_approval))
    builder.add_node("await_approval", RunnableLambda(await_approval))
    builder.add_node("dispatch_job", RunnableLambda(dispatch_job))
    builder.add_node(
        "await_result",
        RunnableLambda(
            build_await_result_node(
                worker,
                gemini_worker,
                openrouter_worker,
                configured_profile_names=profile_names,
            )
        ),
    )
    builder.add_node("await_permission_escalation", RunnableLambda(await_permission_escalation))
    builder.add_node("verify_result", RunnableLambda(verify_result))
    builder.add_node(
        "review_result",
        RunnableLambda(build_review_result_node(worker, gemini_worker, openrouter_worker)),
    )
    builder.add_node("summarize_result", RunnableLambda(summarize_result))
    builder.add_node("persist_memory", RunnableLambda(persist_memory))
    builder.add_edge(START, "ingest_task")
    builder.add_edge("ingest_task", "classify_task")
    builder.add_edge("classify_task", "plan_task")
    builder.add_edge("plan_task", "generate_task_spec")
    builder.add_conditional_edges(
        "generate_task_spec",
        _route_after_generate_task_spec,
        {
            "load_memory": "load_memory",
            "summarize_result": "summarize_result",
        },
    )
    builder.add_edge("load_memory", "choose_worker")
    builder.add_edge("choose_worker", "check_approval")
    builder.add_conditional_edges(
        "check_approval",
        _route_after_check_approval,
        {
            "await_approval": "await_approval",
            "dispatch_job": "dispatch_job",
        },
    )
    builder.add_conditional_edges(
        "await_approval",
        _route_after_await_approval,
        {
            "dispatch_job": "dispatch_job",
            "summarize_result": "summarize_result",
        },
    )
    builder.add_edge("dispatch_job", "await_result")
    builder.add_conditional_edges(
        "await_result",
        _route_after_await_result,
        {
            "await_permission_escalation": "await_permission_escalation",
            "verify_result": "verify_result",
        },
    )
    builder.add_conditional_edges(
        "await_permission_escalation",
        _route_after_await_permission_escalation,
        {
            "dispatch_job": "dispatch_job",
            "verify_result": "verify_result",
        },
    )
    builder.add_edge("verify_result", "review_result")
    builder.add_conditional_edges(
        "review_result",
        _route_after_review_result,
        {
            "dispatch_job": "dispatch_job",
            "summarize_result": "summarize_result",
        },
    )
    builder.add_edge("summarize_result", "persist_memory")
    builder.add_edge("persist_memory", END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
        interrupt_after=interrupt_after,
    )
