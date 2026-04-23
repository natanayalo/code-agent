"""Review-stage node implementation for the orchestrator workflow."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from orchestrator.state import OrchestratorState
from workers import Worker, WorkerRequest
from workers.prompt import build_review_prompt
from workers.review import ReviewFinding, ReviewResult, SuppressedReviewFinding
from workers.review_context import pack_reviewer_context
from workers.self_review import parse_review_result

logger = logging.getLogger(__name__)
DEFAULT_INDEPENDENT_REVIEW_TIMEOUT_SECONDS = 120
DEFAULT_REVIEW_MIN_CONFIDENCE = 0.65
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


def _coerce_positive_int_like(value: object) -> int | None:
    """Parse a positive integer-like input, otherwise return None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        try:
            parsed = int(value)
        except (OverflowError, ValueError):
            return None
        return parsed if parsed > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = int(float(stripped))
        except (OverflowError, ValueError):
            return None
        return parsed if parsed > 0 else None
    return None


def _resolve_review_timeout_seconds(state: OrchestratorState) -> int:
    """Resolve timeout for independent review calls."""
    budget = state.task.budget if isinstance(state.task.budget, dict) else {}
    explicit_timeout = _coerce_positive_int_like(budget.get("independent_review_timeout_seconds"))
    if explicit_timeout is not None:
        return explicit_timeout
    orchestrator_timeout = _coerce_positive_int_like(budget.get("orchestrator_timeout_seconds"))
    if orchestrator_timeout is not None:
        return orchestrator_timeout
    return DEFAULT_INDEPENDENT_REVIEW_TIMEOUT_SECONDS


def _coerce_probability(value: object) -> float | None:
    """Parse numeric confidence threshold values in the closed interval [0, 1]."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        parsed = float(value)
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
    global_min_confidence = _coerce_probability(
        constraints.get("independent_review_min_confidence")
    )
    if global_min_confidence is None:
        global_min_confidence = DEFAULT_REVIEW_MIN_CONFIDENCE
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

        minimum_for_severity = confidence_by_severity.get(severity, global_min_confidence)
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
    return parsed_review.model_copy(
        update={
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


async def review_result(
    state: OrchestratorState,
    *,
    worker_factory: Mapping[str, Worker] | None = None,
) -> dict[str, Any]:
    """Perform an independent advisory review pass after successful verification."""
    # 1. Check if we should skip
    if state.task.constraints.get("skip_independent_review"):
        return {"current_step": "review_result"}

    # Only review successful runs (or warnings)
    if state.verification is None or state.verification.status == "failed":
        return {"current_step": "review_result"}

    if state.result is None:
        return {"current_step": "review_result"}

    # 2. Build the review prompt
    repo_path = _workspace_path_from_result_artifacts(state)
    if repo_path is None:
        logger.warning(
            "Independent review workspace path unavailable; falling back to current directory."
        )

    review_context = pack_reviewer_context(
        task_text=state.normalized_task_text or state.task.task_text,
        worker_summary=state.result.summary or "",
        files_changed=state.result.files_changed,
        diff_text=state.result.diff_text or "",
        commands_run=state.result.commands_run,
        verifier_report=state.verification.model_dump() if state.verification else None,
        session_state=_session_state_for_review_context(state),
    )

    review_prompt = build_review_prompt(
        workspace_path=repo_path or Path("."),
        review_context_packet=review_context,
        reviewer_kind="independent_reviewer",
        task_text=state.normalized_task_text or state.task.task_text,
    )

    # 3. Choose reviewer worker
    # Prefer gemini for review if available, otherwise use the chosen worker
    workers = worker_factory or {}
    reviewer_type = "gemini" if "gemini" in workers else state.dispatch.worker_type
    if not reviewer_type or reviewer_type not in workers:
        logger.warning("No suitable reviewer worker found, skipping independent review.")
        return {"current_step": "review_result"}
    if reviewer_type == state.dispatch.worker_type:
        logger.warning(
            "Independent review is using the same worker type as execution (%s).",
            reviewer_type,
        )

    worker = workers[reviewer_type]

    # 4. Run the review pass
    review_request = WorkerRequest(
        session_id=state.session.session_id if state.session else None,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        task_text="Perform an independent review of the changes.",
        memory_context=state.memory.model_dump(),
        constraints=dict(state.task.constraints),
        budget=dict(state.task.budget),
        secrets=dict(state.task.secrets),
        tools=state.task.tools,
    )

    try:
        # We use the system_prompt override to perform a single-shot review
        review_run_result = await asyncio.wait_for(
            worker.run(review_request, system_prompt=review_prompt),
            timeout=_resolve_review_timeout_seconds(state),
        )
        if review_run_result.status != "success":
            logger.warning(
                "Independent review worker returned non-success status: %s",
                review_run_result.status,
            )

        # 5. Parse findings
        parsed_review = parse_review_result(review_run_result.summary or "")
        if parsed_review is None:
            logger.warning("Independent review output could not be parsed into ReviewResult.")
        if parsed_review:
            # Inject the correct reviewer kind if parser missed it
            if parsed_review.reviewer_kind != "independent_reviewer":
                parsed_review = parsed_review.model_copy(
                    update={"reviewer_kind": "independent_reviewer"}
                )
            parsed_review = _apply_independent_review_suppression(
                parsed_review,
                constraints=state.task.constraints,
            )

            return {
                "current_step": "review_result",
                "review": parsed_review.model_dump(),
                "progress_updates": [*state.progress_updates, "independent review completed"],
            }
    except TimeoutError:
        logger.warning("Independent review pass timed out and was skipped.")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Independent review pass failed unexpectedly.")

    return {"current_step": "review_result"}
