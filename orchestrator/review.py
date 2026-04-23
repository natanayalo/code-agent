"""Review-stage node implementation for the orchestrator workflow."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orchestrator.state import OrchestratorState
from workers import WorkerRequest
from workers.prompt import build_review_prompt
from workers.review_context import pack_reviewer_context
from workers.self_review import parse_review_result

logger = logging.getLogger(__name__)


async def review_result(state: OrchestratorState, *, worker_factory: Any = None) -> dict[str, Any]:
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
    # Use the local repo path if available from the last worker run
    repo_path = None
    if state.result.artifacts:
        for art in state.result.artifacts:
            if art.name == "workspace" and art.uri.startswith("file://"):
                from urllib.parse import unquote, urlparse

                repo_path = Path(unquote(urlparse(art.uri).path))
                break

    review_context = pack_reviewer_context(
        task_text=state.normalized_task_text or state.task.task_text,
        worker_summary=state.result.summary or "",
        files_changed=state.result.files_changed,
        diff_text=state.result.diff_text or "",
        repo_path=repo_path,
        commands_run=state.result.commands_run,
        verifier_report=state.verification.model_dump() if state.verification else None,
        session_state=state.session_state_update.model_dump()
        if state.session_state_update
        else None,
    )

    review_prompt = build_review_prompt(
        workspace_path=repo_path or Path("."),
        review_context_packet=review_context,
        reviewer_kind="independent_reviewer",
        task_text=state.normalized_task_text or state.task.task_text,
    )

    # 3. Choose reviewer worker
    # Prefer gemini for review if available, otherwise use the chosen worker
    reviewer_type = "gemini" if "gemini" in (worker_factory or {}) else state.dispatch.worker_type
    if not reviewer_type or reviewer_type not in (worker_factory or {}):
        logger.warning("No suitable reviewer worker found, skipping independent review.")
        return {"current_step": "review_result"}

    worker = worker_factory[reviewer_type]

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
        review_run_result = await worker.run(review_request, system_prompt=review_prompt)

        # 5. Parse findings
        parsed_review = parse_review_result(review_run_result.summary or "")
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
    except Exception:
        logger.exception("Independent review pass failed unexpectedly.")

    return {"current_step": "review_result"}
