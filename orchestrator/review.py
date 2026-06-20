"""Review-stage node implementation for the orchestrator workflow."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from apps.observability import (
    OPENINFERENCE_SPAN_KIND_ATTRIBUTE,
    SPAN_KIND_CHAIN,
    SPAN_KIND_TOOL,
    set_span_input_output,
    start_optional_span,
)
from orchestrator.state import OrchestratorState
from tools import ToolPermissionLevel
from tools.numeric import coerce_non_negative_int_like, coerce_positive_int_like
from workers import Worker, WorkerRequest, WorkerRuntimeMode
from workers.constants import DEFAULT_REVIEW_TIMEOUT_SECONDS
from workers.prompt_review import build_review_prompt
from workers.review import ReviewFinding, ReviewResult, SuppressedReviewFinding
from workers.review_context import pack_reviewer_context
from workers.self_review import parse_review_result

logger = logging.getLogger(__name__)
DEFAULT_INDEPENDENT_REVIEW_TIMEOUT_SECONDS = DEFAULT_REVIEW_TIMEOUT_SECONDS
DEFAULT_REVIEW_MIN_CONFIDENCE = 0.65
DEFAULT_INDEPENDENT_REVIEW_MAX_REPAIR_PASSES = 1
DEFAULT_REVIEW_MIN_CONFIDENCE_BY_SEVERITY: dict[str, float] = {
    "low": 0.8,
    "medium": 0.7,
    "high": 0.6,
    "critical": 0.5,
}
SEVERITY_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}
DEFAULT_SUPPRESSED_STYLE_CATEGORIES: frozenset[str] = frozenset(
    {"style", "formatting", "naming", "whitespace"}
)
SUPPRESSED_FINDINGS_SUMMARY_PREFIX = "All findings were suppressed by policy thresholds."
REPAIR_REQUEST_CONSTRAINT = "independent_review_repair_request"
REPAIR_PASSES_USED_CONSTRAINT = "independent_review_repair_passes_used"
REPAIR_MAX_PASSES_CONSTRAINT = "independent_review_max_repair_passes"
SKIP_INDEPENDENT_REVIEW_CONSTRAINT = "skip_independent_review"
ENABLE_REPAIR_HANDOFF_CONSTRAINT = "independent_review_enable_repair_handoff"


def _resolve_review_timeout_seconds(state: OrchestratorState) -> int:
    """Resolve timeout for independent review calls."""
    budget = state.task.budget if isinstance(state.task.budget, dict) else {}
    return (
        coerce_positive_int_like(budget.get("independent_review_timeout_seconds"))
        or coerce_positive_int_like(budget.get("orchestrator_timeout_seconds"))
        or DEFAULT_INDEPENDENT_REVIEW_TIMEOUT_SECONDS
    )


def _coerce_probability(value: object) -> float | None:
    """Parse numeric confidence threshold values in the closed interval [0, 1]."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            parsed = float(value)
        except OverflowError:
            return None
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = float(stripped)
        except (OverflowError, ValueError):
            return None
    else:
        return None

    if 0.0 <= parsed <= 1.0:
        return parsed
    return None


def _coerce_string_set(value: object) -> set[str]:
    """Normalize list-like or comma-delimited category strings into a lowercase set."""
    if isinstance(value, str):
        raw_values = value.split(",")
    elif isinstance(value, list | tuple | set):
        raw_values = list(value)
    else:
        return set()

    normalized: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip().lower()
        if cleaned:
            normalized.add(cleaned)
    return normalized


def _review_min_confidence_by_severity(constraints: Mapping[str, Any]) -> dict[str, float]:
    """Resolve severity-specific review confidence thresholds with safe defaults."""
    resolved = dict(DEFAULT_REVIEW_MIN_CONFIDENCE_BY_SEVERITY)
    explicit_global = _coerce_probability(constraints.get("independent_review_min_confidence"))
    if explicit_global is not None:
        resolved = {severity: explicit_global for severity in SEVERITY_RANK}
    raw_thresholds = constraints.get("independent_review_min_confidence_by_severity")
    if not isinstance(raw_thresholds, Mapping):
        return resolved

    for severity, raw_value in raw_thresholds.items():
        if not isinstance(severity, str):
            continue
        severity_key = severity.strip().lower()
        if severity_key not in SEVERITY_RANK:
            continue
        parsed = _coerce_probability(raw_value)
        if parsed is not None:
            resolved[severity_key] = parsed
    return resolved


def _resolve_review_min_severity(constraints: Mapping[str, Any]) -> str | None:
    """Resolve the minimum surfaced severity level, when configured."""
    raw = constraints.get("independent_review_min_severity")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    if normalized in SEVERITY_RANK:
        return normalized
    return None


def _resolve_style_categories(constraints: Mapping[str, Any]) -> set[str]:
    """Resolve which finding categories should be treated as style-only by default."""
    if constraints.get("independent_review_include_style_findings") is True:
        return set()

    configured = _coerce_string_set(constraints.get("independent_review_style_categories"))
    if configured:
        return configured
    return set(DEFAULT_SUPPRESSED_STYLE_CATEGORIES)


def _apply_independent_review_suppression(
    parsed_review: ReviewResult,
    *,
    constraints: Mapping[str, Any],
) -> ReviewResult:
    """Suppress low-value findings before surfacing independent review results."""
    confidence_by_severity = _review_min_confidence_by_severity(constraints)
    min_severity = _resolve_review_min_severity(constraints)
    suppressed_style_categories = _resolve_style_categories(constraints)

    filtered_findings: list[ReviewFinding] = []
    suppressed_findings: list[SuppressedReviewFinding] = list(parsed_review.suppressed_findings)

    for finding in parsed_review.findings:
        reasons: list[str] = []
        severity = finding.severity
        category = finding.category.strip().lower()

        if category in suppressed_style_categories:
            reasons.append(f"style category suppressed by policy ({category})")

        minimum_for_severity = confidence_by_severity.get(severity, DEFAULT_REVIEW_MIN_CONFIDENCE)
        if finding.confidence < minimum_for_severity:
            reasons.append(
                "confidence below effective threshold "
                f"for {severity} ({finding.confidence:.2f} < {minimum_for_severity:.2f})"
            )

        if min_severity is not None and SEVERITY_RANK[severity] < SEVERITY_RANK[min_severity]:
            reasons.append(f"severity below threshold ({severity} < {min_severity})")

        if reasons:
            suppressed_findings.append(SuppressedReviewFinding(finding=finding, reasons=reasons))
            continue
        filtered_findings.append(finding)

    filtered_outcome = "findings" if filtered_findings else "no_findings"
    summary = parsed_review.summary
    if parsed_review.outcome == "findings" and filtered_outcome == "no_findings":
        summary = f"{SUPPRESSED_FINDINGS_SUMMARY_PREFIX} Original reviewer summary: {summary}"
    return parsed_review.model_copy(
        update={
            "summary": summary,
            "outcome": filtered_outcome,
            "findings": filtered_findings,
            "suppressed_findings": suppressed_findings,
        }
    )


def _workspace_path_from_result_artifacts(state: OrchestratorState) -> Path | None:
    """Resolve workspace artifact URI to a local path for review prompt context."""
    if not state.result or not state.result.artifacts:
        return None

    for art in state.result.artifacts:
        if art.name != "workspace" or not art.uri.startswith("file://"):
            continue
        parsed = urlparse(art.uri)
        decoded_path = unquote(parsed.path)
        path_text = url2pathname(decoded_path)
        # Handle file:///C:/... style URIs robustly across host OSes.
        if (
            len(path_text) >= 3
            and path_text[0] == "/"
            and path_text[1].isalpha()
            and path_text[2] == ":"
        ):
            path_text = path_text[1:]
        return Path(path_text)
    return None


def _session_state_for_review_context(state: OrchestratorState) -> Mapping[str, Any] | None:
    """Provide compact session context for review, even before summarize_result."""
    if state.session_state_update is not None:
        return state.session_state_update.model_dump()
    if state.result is None:
        return None
    active_goal = state.normalized_task_text or state.task.task_text
    return {
        "active_goal": active_goal,
        "files_touched": list(state.result.files_changed),
    }


def _resolve_repair_handoff_budget(constraints: Mapping[str, Any]) -> tuple[int, int]:
    """Resolve bounded independent-review repair-loop settings."""
    max_passes = coerce_non_negative_int_like(constraints.get(REPAIR_MAX_PASSES_CONSTRAINT))
    if max_passes is None:
        max_passes = DEFAULT_INDEPENDENT_REVIEW_MAX_REPAIR_PASSES
    used_passes = coerce_non_negative_int_like(constraints.get(REPAIR_PASSES_USED_CONSTRAINT))
    if used_passes is None:
        used_passes = 0
    return max_passes, used_passes


def _build_review_repair_task_text(*, task_text: str, findings: list[ReviewFinding]) -> str:
    """Create a focused repair instruction from actionable review findings."""
    lines = [
        "Apply targeted code fixes for independent review findings.",
        "Keep changes minimal and limited to the issues listed below.",
        f"Original task objective: {task_text}",
        "",
        "Actionable findings:",
    ]
    for index, finding in enumerate(findings, start=1):
        location = finding.file_path
        if finding.line_start is not None:
            location = f"{location}:{finding.line_start}"
        lines.extend(
            [
                (
                    f"{index}. [{finding.severity.upper()}] {finding.title} "
                    f"(confidence={finding.confidence:.2f}, location={location})"
                ),
                f"   Impact: {finding.why_it_matters}",
            ]
        )
    lines.extend(
        [
            "",
            "After applying fixes, run the smallest relevant verification commands and summarize.",
        ]
    )
    return "\n".join(lines)


def _cleanup_repair_handoff_constraints(constraints: Mapping[str, Any]) -> dict[str, Any]:
    """Drop transient repair handoff constraint fields after the repair attempt completes."""
    cleaned = dict(constraints)
    cleaned.pop(REPAIR_REQUEST_CONSTRAINT, None)
    cleaned.pop(SKIP_INDEPENDENT_REVIEW_CONSTRAINT, None)
    return cleaned


def _build_review_request(state: OrchestratorState) -> WorkerRequest:
    constraints = dict(state.task.constraints)
    constraints["read_only"] = False
    if constraints.get("granted_permission") != ToolPermissionLevel.WORKSPACE_WRITE:
        constraints.pop("granted_permission", None)

    return WorkerRequest(
        session_id=state.session.session_id if state.session else None,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        workspace_id=state.dispatch.workspace_id
        or (state.result.workspace_id if state.result else None),
        task_text=(
            "Perform an independent review of the changes in the workspace. "
            "Use your tools (git diff, read_file) to inspect the modified files "
            "and their impact before providing your final review findings."
        ),
        memory_context=state.memory.model_dump(),
        constraints=constraints,
        budget=dict(state.task.budget),
        secrets=dict((state.task.secrets or {}) | {"POETRY_VIRTUALENVS_IN_PROJECT": "true"}),
        tools=state.task.tools,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
    )


async def _execute_independent_reviewer(
    worker_type: str,
    worker: Worker,
    review_request: WorkerRequest,
    review_prompt: str,
    state: OrchestratorState,
    timeout_seconds: int,
) -> ReviewResult | None:
    try:
        with start_optional_span(
            tracer_name="orchestrator.review",
            span_name=f"independent_reviewer.{worker_type}",
            task_id=state.task.task_id,
            session_id=state.session.session_id if state.session else None,
            attempt=state.attempt_count,
            task_kind=state.task_kind,
            route_reason=state.route.route_reason if state.route else None,
            verification_summary=state.verification.summary if state.verification else None,
            attributes={OPENINFERENCE_SPAN_KIND_ATTRIBUTE: SPAN_KIND_TOOL},
        ):
            review_run_result = await asyncio.wait_for(
                worker.run(review_request, system_prompt=review_prompt),
                timeout=timeout_seconds,
            )
            set_span_input_output(input_data=None, output_data=review_run_result.summary)

        if review_run_result.status != "success":
            logger.warning(
                "Independent review worker returned non-success status: %s",
                review_run_result.status,
                extra={"task_id": state.task.task_id, "worker_type": worker_type},
            )

        parsed_review = parse_review_result(
            review_run_result.json_payload or review_run_result.summary or ""
        )
        if parsed_review is None:
            logger.warning("Independent review output could not be parsed into ReviewResult.")
            if review_run_result.status != "success" or review_run_result.failure_kind in {
                "provider_error",
                "provider_auth",
                "sandbox_infra",
                "timeout",
                "model_error",
                "unknown",
            }:
                logger.info(
                    f"Retrying independent review with next worker after {worker_type} failure",
                    extra={"task_id": state.task.task_id},
                )
            return None
        return parsed_review

    except TimeoutError:
        logger.warning("Independent review pass timed out and was skipped.")
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(f"Independent review pass failed unexpectedly for {worker_type}.")
        return None


def _repair_handoff_update(
    state: OrchestratorState,
    parsed_review: ReviewResult,
) -> dict[str, Any] | None:
    """Build state updates that hand off a bounded review-driven repair pass."""
    if not parsed_review.findings:
        return None

    constraints = state.task.constraints
    enable_handoff = constraints.get(ENABLE_REPAIR_HANDOFF_CONSTRAINT)

    # Enable by default for high-severity findings if not explicitly disabled
    has_high_severity = any(f.severity in ("high", "critical") for f in parsed_review.findings)
    if enable_handoff is False:
        return None
    if enable_handoff is not True and not has_high_severity:
        return None

    max_passes, used_passes = _resolve_repair_handoff_budget(constraints)
    if used_passes >= max_passes:
        return None

    task_text = state.normalized_task_text or state.task.task_text
    repair_task_text = _build_review_repair_task_text(
        task_text=task_text,
        findings=parsed_review.findings,
    )
    updated_constraints = dict(constraints)
    updated_constraints[REPAIR_REQUEST_CONSTRAINT] = repair_task_text
    updated_constraints[REPAIR_PASSES_USED_CONSTRAINT] = used_passes + 1
    # Prevent builder-reviewer ping-pong once the repair budget is exhausted.
    updated_constraints[SKIP_INDEPENDENT_REVIEW_CONSTRAINT] = used_passes + 1 >= max_passes
    updated_task = state.task.model_copy(update={"constraints": updated_constraints})

    return {
        "current_step": "review_result",
        "task": updated_task.model_dump(),
        "review": parsed_review.model_dump(),
        "verification": None,
        "repair_handoff_requested": True,
        "progress_updates": [
            *state.progress_updates,
            "independent review requested one bounded repair handoff",
        ],
    }


def _check_skip_review(state: OrchestratorState) -> dict[str, Any] | None:
    is_read_only = state.task.constraints.get("read_only") is True
    no_files_changed = not (state.result and state.result.files_changed)

    if is_read_only or no_files_changed:
        reason = "read-only task" if is_read_only else "no files changed"
        updated_task = state.task
        if state.task.constraints.get(REPAIR_REQUEST_CONSTRAINT) is not None:
            updated_task = state.task.model_copy(
                update={
                    "constraints": _cleanup_repair_handoff_constraints(state.task.constraints),
                }
            )

        return {
            "current_step": "review_result",
            "task": updated_task.model_dump(),
            "review": None,
            "progress_updates": [
                *state.progress_updates,
                f"independent code-change review skipped ({reason})",
            ],
        }

    if (
        state.task.constraints.get(REPAIR_REQUEST_CONSTRAINT) is not None
        and state.task.constraints.get(SKIP_INDEPENDENT_REVIEW_CONSTRAINT) is True
    ):
        updated_task = state.task.model_copy(
            update={
                "constraints": _cleanup_repair_handoff_constraints(state.task.constraints),
            }
        )
        return {
            "current_step": "review_result",
            "task": updated_task.model_dump(),
            "review": None,
            "progress_updates": [
                *state.progress_updates,
                "independent review skipped after repair",
            ],
        }

    # 1. Check if we should skip
    if state.task.constraints.get(SKIP_INDEPENDENT_REVIEW_CONSTRAINT):
        return {"current_step": "review_result"}

    # Only review successful runs (or warnings)
    if state.verification is None or state.verification.status == "failed":
        return {"current_step": "review_result"}

    if state.result is None:
        return {"current_step": "review_result"}

    return None


def _get_reviewer_workers(
    state: OrchestratorState, worker_factory: Mapping[str, Worker] | None
) -> list[tuple[str, Worker]]:
    reviewer_workers: list[tuple[str, Worker]] = []
    available_workers = worker_factory or {}
    if "antigravity" in available_workers:
        reviewer_workers.append(("antigravity", available_workers["antigravity"]))
    if "codex" in available_workers:
        reviewer_workers.append(("codex", available_workers["codex"]))
    if not reviewer_workers and state.dispatch.worker_type is not None:
        dw_type = state.dispatch.worker_type
        if dw_type in available_workers:
            reviewer_workers.append((dw_type, available_workers[dw_type]))
    return reviewer_workers


def _build_review_prompt_for_state(state: OrchestratorState) -> str:
    repo_path = _workspace_path_from_result_artifacts(state)
    if repo_path is None:
        logger.warning(
            "Independent review workspace path unavailable; falling back to current directory."
        )

    review_context = pack_reviewer_context(
        task_text=state.normalized_task_text or state.task.task_text,
        worker_summary=state.result.summary or "",  # type: ignore[union-attr]
        files_changed=state.result.files_changed,  # type: ignore[union-attr]
        # T-065: Omit diff_text to encourage tool-based exploration as per user feedback.
        diff_text=None,
        commands_run=state.result.commands_run,  # type: ignore[union-attr]
        verifier_report=state.verification.model_dump() if state.verification else None,
        session_state=_session_state_for_review_context(state),
    )

    return build_review_prompt(
        workspace_path=repo_path or Path("."),
        review_context_packet=review_context,
        reviewer_kind="independent_reviewer",
        task_text=state.normalized_task_text or state.task.task_text,
    )


async def review_result(
    state: OrchestratorState,
    *,
    worker_factory: Mapping[str, Worker] | None = None,
) -> dict[str, Any]:
    """Perform an independent advisory review pass after successful verification."""
    skip_decision = _check_skip_review(state)
    if skip_decision is not None:
        return skip_decision

    review_prompt = _build_review_prompt_for_state(state)
    reviewer_workers = _get_reviewer_workers(state, worker_factory)

    if not reviewer_workers:
        logger.warning("No suitable reviewer worker found, skipping independent review.")
        return {"current_step": "review_result"}

    # 4. Run the review pass with fallback
    review_request = _build_review_request(state)
    timeout_seconds = _resolve_review_timeout_seconds(state)

    with start_optional_span(
        tracer_name="orchestrator.graph",
        span_name="orchestrator.node.review_result",
        attributes={OPENINFERENCE_SPAN_KIND_ATTRIBUTE: SPAN_KIND_CHAIN},
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
        task_kind=state.task_kind,
        route_reason=state.route.route_reason if state.route else None,
        verification_summary=state.verification.summary if state.verification else None,
    ):
        for i, (worker_type, worker) in enumerate(reviewer_workers):
            if worker_type == state.dispatch.worker_type:
                logger.warning(
                    f"Independent review is using the same worker type "
                    f"as execution ({worker_type})."
                )

            parsed_review = await _execute_independent_reviewer(
                worker_type, worker, review_request, review_prompt, state, timeout_seconds
            )
            if parsed_review is None:
                if i < len(reviewer_workers) - 1:
                    continue
                return {"current_step": "review_result"}

            # 5. Process findings
            if parsed_review.reviewer_kind != "independent_reviewer":
                parsed_review = parsed_review.model_copy(
                    update={"reviewer_kind": "independent_reviewer"}
                )
            parsed_review = _apply_independent_review_suppression(
                parsed_review,
                constraints=state.task.constraints,
            )
            repair_update = _repair_handoff_update(state, parsed_review)
            if repair_update is not None:
                return repair_update

            return {
                "current_step": "review_result",
                "review": parsed_review.model_dump(),
                "progress_updates": [*state.progress_updates, "independent review completed"],
            }

        return {"current_step": "review_result"}
