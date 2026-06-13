"""Delivery node implementation for GitHub branch and PR integration."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from apps.observability import (
    NATIVE_AGENT_STDERR_ATTRIBUTE,
    NATIVE_AGENT_STDOUT_ATTRIBUTE,
    SPAN_KIND_CHAIN,
    set_current_span_attribute,
    set_span_input_output,
    start_optional_span,
)
from db.enums import TimelineEventType, WorkerRunStatus
from orchestrator.nodes.utils import (
    _available_workers,
    _ensure_state,
    _progress_update,
    _timeline_event,
)
from orchestrator.state import OrchestratorState
from workers.base import Worker, WorkerRequest, WorkerResult

logger = logging.getLogger(__name__)


def _build_delivery_prompt(
    state: OrchestratorState,
    branch_name: str,
    pr_title: str,
    pr_body: str,
) -> str:
    """Construct the agent prompt to execute the delivery."""
    delivery_mode = state.task_spec.delivery_mode if state.task_spec else "workspace"

    # Return a natural language prompt for the agent instead of a bash script.
    mode_instructions = ""
    if delivery_mode == "draft_pr":
        mode_instructions = (
            f"After pushing, use the github tool or gh cli to create a draft PR titled "
            f"'{pr_title}' with body '{pr_body}'. If it already exists, do not error."
        )

    prompt = f"""
Please deliver the current workspace changes to the remote repository.

Configuration:
- Target branch: {branch_name}
- Delivery mode: {delivery_mode}

Instructions:
1. Fetch the latest from origin.
2. Checkout or create the branch `{branch_name}`.
3. Check for any uncommitted changes using `git status`.
   - Before committing, review the changes to ensure no unintended files
     (like debug logs, temporary artifacts, or secrets) are included.
   - Stage ONLY the files relevant to this task (avoid blindly using `git add .`).
   - Commit them locally on `{branch_name}` with a clear, specific message
     describing the work done.
4. If the remote branch exists, gracefully rebase your changes onto it.
   Resolve any conflicts professionally.
5. Push the changes to origin (`git push -u origin {branch_name}`).
{mode_instructions}

Do not use `--force` or `-f` when pushing.
Never use `--no-verify` to bypass git hooks or pre-commit checks.
If a hook fails, you must fix the underlying issue.
If you encounter any unresolvable conflicts, gracefully exit and explain the failure.
"""
    return prompt.strip()


async def _run_deliver_result(
    state_input: OrchestratorState,
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
    shell_worker: Worker | None = None,
) -> dict[str, Any]:
    state = _ensure_state(state_input)

    # Check preconditions
    if not state.result or state.result.status != WorkerRunStatus.SUCCESS:
        return {"current_step": "deliver_result"}

    if not state.task_spec or state.task_spec.delivery_mode not in {"branch", "draft_pr"}:
        return {"current_step": "deliver_result"}

    if not state.dispatch or not state.dispatch.workspace_id:
        return {"current_step": "deliver_result"}

    available = _available_workers(worker, gemini_worker, openrouter_worker, shell_worker)

    # Try to use the worker that was dispatched for the task.
    # Fallback to gemini if it's shell or not found.
    worker_id = state.dispatch.worker_type if state.dispatch else "gemini"
    if worker_id == "shell" or worker_id not in available:
        worker_id = "gemini"

    delivery_worker = available.get(worker_id)
    if not delivery_worker:
        msg = f"Delivery failed: no suitable delivery worker configured (tried {worker_id})."
        logger.warning(msg)
        return {
            "current_step": "deliver_result",
            "progress_updates": _progress_update(
                state, f"delivery failed (missing worker {worker_id})"
            ),
            "result": WorkerResult(status=WorkerRunStatus.FAILURE, summary=msg),
            **_timeline_event(
                state,
                TimelineEventType.DELIVERY_FAILED,
                message=msg,
            ),
        }

    with start_optional_span(
        tracer_name="orchestrator.graph",
        span_name="orchestrator.node.deliver_result",
        attributes={"openinference.span.kind": SPAN_KIND_CHAIN},
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
    ):
        logger.info(
            "Delivering task result",
            extra={
                "task_id": state.task.task_id,
                "delivery_mode": state.task_spec.delivery_mode,
            },
        )

        task_secrets = state.task.secrets or {}
        gh_token = (
            task_secrets.get("GH_TOKEN")
            or task_secrets.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
        )
        if not gh_token and state.task_spec.delivery_mode == "draft_pr":
            msg = (
                "Delivery failed: GH_TOKEN or GITHUB_TOKEN not found in environment "
                "(required for PR creation)."
            )
            logger.warning(msg)
            return {
                "current_step": "deliver_result",
                "progress_updates": _progress_update(
                    state, "delivery failed (missing github token)"
                ),
                "result": WorkerResult(status=WorkerRunStatus.FAILURE, summary=msg),
                **_timeline_event(
                    state,
                    TimelineEventType.DELIVERY_FAILED,
                    message=msg,
                ),
            }

        branch_name = state.task_spec.delivery_branch or f"task/{state.task.task_id}"
        if branch_name in {"master", "main"}:
            msg = (
                f"Delivery failed: committing or pushing directly to protected "
                f"branch '{branch_name}' is forbidden."
            )
            logger.warning(msg)
            return {
                "current_step": "deliver_result",
                "progress_updates": _progress_update(
                    state, f"delivery failed (forbidden branch {branch_name})"
                ),
                "result": WorkerResult(status=WorkerRunStatus.FAILURE, summary=msg),
                **_timeline_event(
                    state,
                    TimelineEventType.DELIVERY_FAILED,
                    message=msg,
                ),
            }
        pr_title = (
            state.task_spec.pr_title or f"Automated implementation for task {state.task.task_id}"
        )
        pr_body = (
            state.task_spec.pr_body
            or f"Automated PR created by code agent for task {state.task.task_id}."
        )
        prompt = _build_delivery_prompt(state, branch_name, pr_title, pr_body)

        request = WorkerRequest(
            session_id=state.session.session_id if state.session else None,
            task_id=state.task.task_id,
            repo_url=state.task.repo_url,
            branch=state.task.branch,
            workspace_id=state.dispatch.workspace_id,
            task_text=prompt,
            constraints=dict(state.task.constraints or {}),
            budget={
                "worker_timeout_seconds": 300,
                **(state.task.budget or {}),
            },
            network_enabled=True,
            secrets={
                **task_secrets,
                "GH_TOKEN": gh_token or "",
                "GITHUB_TOKEN": gh_token or "",
            },
            tools=["execute_bash", "execute_git", "execute_github"],
            worker_profile=(
                state.dispatch.worker_profile
                or (state.route.chosen_profile if state.route else None)
            ),
            runtime_mode=(
                state.dispatch.runtime_mode or (state.route.runtime_mode if state.route else None)
            ),
        )

        set_span_input_output(
            input_data={
                "delivery_mode": state.task_spec.delivery_mode,
                "branch": branch_name,
                "worker": worker_id,
            }
        )

        try:
            result = await delivery_worker.run(request)
        except Exception as exc:
            msg = f"Delivery execution failed: {type(exc).__name__}: {exc}"
            logger.debug(msg)
            return {
                "current_step": "deliver_result",
                "progress_updates": _progress_update(state, "delivery execution failed"),
                "result": WorkerResult(status=WorkerRunStatus.FAILURE, summary=msg),
                **_timeline_event(
                    state,
                    TimelineEventType.DELIVERY_FAILED,
                    message=msg,
                ),
            }

        if hasattr(result, "stdout") and result.stdout:
            set_current_span_attribute(NATIVE_AGENT_STDOUT_ATTRIBUTE, result.stdout)
        if hasattr(result, "stderr") and result.stderr:
            set_current_span_attribute(NATIVE_AGENT_STDERR_ATTRIBUTE, result.stderr)

        if getattr(result, "status", None) != WorkerRunStatus.SUCCESS:
            msg = f"Delivery script failed: {getattr(result, 'summary', '')}"
            return {
                "current_step": "deliver_result",
                "progress_updates": _progress_update(state, "delivery script failed"),
                "result": WorkerResult(status=WorkerRunStatus.FAILURE, summary=msg),
                **_timeline_event(
                    state,
                    TimelineEventType.DELIVERY_FAILED,
                    message=msg,
                    payload={
                        "stdout": getattr(result, "stdout", ""),
                        "stderr": getattr(result, "stderr", ""),
                    },
                ),
            }

        set_span_input_output(output_data="success")
        return {
            "current_step": "deliver_result",
            "progress_updates": _progress_update(state, "delivery completed"),
            **_timeline_event(
                state,
                TimelineEventType.DELIVERY_COMPLETED,
                message=f"Successfully delivered result via {state.task_spec.delivery_mode}",
                payload={"branch": branch_name, "pr_title": pr_title},
            ),
        }


def build_deliver_result_node(
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
    shell_worker: Worker | None = None,
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """Factory for the delivery node."""
    import functools

    return functools.partial(
        _run_deliver_result,
        worker=worker,
        gemini_worker=gemini_worker,
        openrouter_worker=openrouter_worker,
        shell_worker=shell_worker,
    )
