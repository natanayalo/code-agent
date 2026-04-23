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
from workers.review_context import pack_reviewer_context
from workers.self_review import parse_review_result

logger = logging.getLogger(__name__)
DEFAULT_INDEPENDENT_REVIEW_TIMEOUT_SECONDS = 120


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
            asyncio.shield(worker.run(review_request, system_prompt=review_prompt)),
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
