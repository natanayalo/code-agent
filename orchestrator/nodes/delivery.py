"""Delivery node implementation for GitHub branch and PR integration."""

from __future__ import annotations

import json
import logging
import os
import subprocess
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
from orchestrator.github_repo import github_repo_spec_from_url
from orchestrator.nodes.utils import (
    _available_workers,
    _ensure_state,
    _progress_update,
    _timeline_event,
)
from orchestrator.runtime_manifest import build_runtime_manifest
from orchestrator.state import OrchestratorState
from workers.base import Worker, WorkerRequest, WorkerResult

logger = logging.getLogger(__name__)


def _is_valid_git_branch_name(name: str) -> bool:
    """Validate a branch name according to git check-ref-format --branch semantics."""
    if not name or name.startswith("-"):
        return False
    if name == "@":
        return False
    if name.startswith("/") or name.endswith("/") or "//" in name:
        return False
    if name.endswith("."):
        return False

    invalid_chars = [" ", "\t", "\n", "~", "^", ":", "?", "*", "[", "\\"]
    if any(c in name for c in invalid_chars):
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return False

    if ".." in name or "@{" in name:
        return False

    for component in name.split("/"):
        if component.startswith(".") or component.endswith(".lock"):
            return False

    return True


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


def _delivery_failure_response(
    state: OrchestratorState,
    msg: str,
    progress_message: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "current_step": "deliver_result",
        "progress_updates": _progress_update(state, progress_message),
        "result": WorkerResult(status="failure", summary=msg),
        **_timeline_event(
            state,
            TimelineEventType.DELIVERY_FAILED,
            message=msg,
            payload=payload,
        ),
    }


def _select_delivery_worker(
    state: OrchestratorState, available: dict[str, Worker]
) -> tuple[str, Worker | None]:
    # Try to use the worker that was dispatched for the task.
    # Fallback to Antigravity if it's shell or not found.
    worker_id = state.dispatch.worker_type if state.dispatch else "antigravity"
    if worker_id == "shell" or worker_id not in available:
        worker_id = "antigravity"
    return worker_id, available.get(worker_id)


def _delivery_github_token(state: OrchestratorState) -> str | None:
    task_secrets = state.task.secrets or {}
    return (
        task_secrets.get("GH_TOKEN")
        or task_secrets.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )


def _validated_delivery_branch(
    state: OrchestratorState,
) -> tuple[str | None, dict[str, Any] | None]:
    assert state.task_spec is not None
    branch_name = (state.task_spec.delivery_branch or f"task/{state.task.task_id}").strip()

    if not _is_valid_git_branch_name(branch_name):
        msg = f"Delivery failed: branch name '{branch_name}' is invalid or unsafe."
        logger.warning(msg)
        return branch_name, _delivery_failure_response(
            state,
            msg,
            "delivery failed (invalid branch name)",
        )

    if branch_name in {"master", "main"}:
        msg = (
            f"Delivery failed: committing or pushing directly to protected "
            f"branch '{branch_name}' is forbidden."
        )
        logger.warning(msg)
        return branch_name, _delivery_failure_response(
            state,
            msg,
            f"delivery failed (forbidden branch {branch_name})",
        )

    return branch_name, None


def _delivery_pr_fields(state: OrchestratorState) -> tuple[str, str]:
    assert state.task_spec is not None
    pr_title = state.task_spec.pr_title or (
        f"Automated implementation for task {state.task.task_id}"
    )
    pr_body = state.task_spec.pr_body or (
        f"Automated PR created by code agent for task {state.task.task_id}."
    )
    return pr_title, pr_body


def _build_delivery_worker_request(
    state: OrchestratorState,
    *,
    prompt: str,
    gh_token: str | None,
) -> WorkerRequest:
    assert state.dispatch is not None
    task_secrets = state.task.secrets or {}
    worker_profile = state.dispatch.worker_profile or (
        state.route.chosen_profile if state.route else None
    )
    runtime_mode = state.dispatch.runtime_mode or (
        state.route.runtime_mode if state.route else None
    )
    worker_type = state.dispatch.worker_type or (state.route.chosen_worker if state.route else None)
    budget = {
        "worker_timeout_seconds": 300,
        **(state.task.budget or {}),
    }
    runtime_manifest = build_runtime_manifest(
        worker_type=worker_type,
        worker_profile=worker_profile,
        runtime_mode=runtime_mode,
        workspace_id=state.dispatch.workspace_id,
        task_spec=state.task_spec,
        read_only=False,
        network_enabled=True,
        budget=budget,
        requested_tools=["execute_bash", "execute_git", "execute_github"],
    ).model_dump(mode="json")
    return WorkerRequest(
        session_id=state.session.session_id if state.session else None,
        task_id=state.task.task_id,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        workspace_id=state.dispatch.workspace_id,
        task_text=prompt,
        constraints=dict(state.task.constraints or {}),
        budget=budget,
        network_enabled=True,
        secrets={
            **task_secrets,
            "GH_TOKEN": gh_token or "",
            "GITHUB_TOKEN": gh_token or "",
        },
        tools=["execute_bash", "execute_git", "execute_github"],
        worker_profile=worker_profile,
        runtime_mode=runtime_mode,
        runtime_manifest=runtime_manifest,
    )


def _capture_delivery_metadata(
    state: OrchestratorState,
    branch_name: str,
    gh_token: str | None,
) -> dict[str, Any] | None:
    if not state.task_spec or not state.task.repo_url:
        return None

    delivery_mode = state.task_spec.delivery_mode
    metadata = {
        "delivery_mode": delivery_mode,
        "branch_name": branch_name,
    }

    if delivery_mode != "draft_pr":
        return metadata

    if not gh_token:
        return metadata

    repo_spec = github_repo_spec_from_url(state.task.repo_url)
    if repo_spec is None:
        logger.debug("Failed to derive GitHub repo spec from repo_url for delivery metadata.")
        return metadata

    env = os.environ.copy()
    env["GH_TOKEN"] = gh_token

    cmd = [
        "gh",
        "pr",
        "view",
        branch_name,
        "-R",
        repo_spec,
        "--json",
        "url,number,headRefOid,headRefName",
    ]
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)

        metadata["pr_url"] = data.get("url")
        metadata["pr_number"] = data.get("number")
        metadata["head_sha"] = data.get("headRefOid")
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        logger.debug("Failed to capture PR metadata via gh cli: %s", e)

    return metadata


def _record_delivery_worker_output(result: WorkerResult) -> None:
    if hasattr(result, "stdout") and result.stdout:
        set_current_span_attribute(NATIVE_AGENT_STDOUT_ATTRIBUTE, result.stdout)
    if hasattr(result, "stderr") and result.stderr:
        set_current_span_attribute(NATIVE_AGENT_STDERR_ATTRIBUTE, result.stderr)


def _merge_delivery_result(
    implementation_result: WorkerResult | None, delivery_result: WorkerResult
) -> WorkerResult:
    if implementation_result is None:
        return delivery_result

    new_json = {
        **(implementation_result.json_payload or {}),
        **(delivery_result.json_payload or {}),
    }
    summary_parts = []
    if implementation_result.summary:
        summary_parts.append(implementation_result.summary)
    if delivery_result.summary:
        summary_parts.append(f"Delivery Output:\n{delivery_result.summary}")
    merged_summary = "\n\n".join(summary_parts) or "Delivery completed."

    return implementation_result.model_copy(
        update={
            "artifacts": implementation_result.artifacts + (delivery_result.artifacts or []),
            "summary": merged_summary,
            "json_payload": new_json,
        }
    )


def _should_deliver_result(state: OrchestratorState) -> bool:
    if not state.result or state.result.status != WorkerRunStatus.SUCCESS:
        return False
    if not state.task_spec or state.task_spec.delivery_mode not in {"branch", "draft_pr"}:
        return False
    return bool(state.dispatch and state.dispatch.workspace_id)


def _log_delivery_start(state: OrchestratorState) -> None:
    logger.info(
        "Delivering task result",
        extra={
            "task_id": state.task.task_id,
            "delivery_mode": state.task_spec.delivery_mode if state.task_spec else None,
        },
    )


async def _run_delivery_worker(
    state: OrchestratorState,
    delivery_worker: Worker,
    request: WorkerRequest,
) -> WorkerResult | dict[str, Any]:
    try:
        result = await delivery_worker.run(request)
    except Exception as exc:
        msg = f"Delivery execution failed: {type(exc).__name__}: {exc}"
        logger.debug(msg)
        return _delivery_failure_response(state, msg, "delivery execution failed")

    _record_delivery_worker_output(result)

    if getattr(result, "status", None) == WorkerRunStatus.SUCCESS:
        return result

    msg = f"Delivery script failed: {getattr(result, 'summary', '')}"
    return _delivery_failure_response(
        state,
        msg,
        "delivery script failed",
        payload={
            "stdout": getattr(result, "stdout", ""),
            "stderr": getattr(result, "stderr", ""),
        },
    )


def _delivery_completed_response(
    state: OrchestratorState,
    *,
    branch_name: str,
    pr_title: str,
    merged_result: WorkerResult,
) -> dict[str, Any]:
    assert state.task_spec is not None
    set_span_input_output(None, output_data="success")
    return {
        "current_step": "deliver_result",
        "progress_updates": _progress_update(state, "delivery completed"),
        "result": merged_result,
        **_timeline_event(
            state,
            TimelineEventType.DELIVERY_COMPLETED,
            message=f"Successfully delivered result via {state.task_spec.delivery_mode}",
            payload={"branch": branch_name, "pr_title": pr_title},
        ),
    }


async def _delivery_success_response(
    state: OrchestratorState,
    delivery_result: WorkerResult,
    branch_name: str,
    pr_title: str,
    gh_token: str | None,
) -> dict[str, Any]:
    import asyncio

    merged_result = _merge_delivery_result(state.result, delivery_result)
    delivery_metadata = await asyncio.to_thread(
        _capture_delivery_metadata, state, branch_name, gh_token
    )
    if delivery_metadata:
        merged_result.delivery_metadata = delivery_metadata
    return _delivery_completed_response(
        state,
        branch_name=branch_name,
        pr_title=pr_title,
        merged_result=merged_result,
    )


async def _run_deliver_result(
    state_input: OrchestratorState,
    worker: Worker | None = None,
    gemini_worker: Worker | None = None,
    openrouter_worker: Worker | None = None,
    shell_worker: Worker | None = None,
) -> dict[str, Any]:
    state = _ensure_state(state_input)

    if not _should_deliver_result(state):
        return {"current_step": "deliver_result"}
    assert state.task_spec is not None
    assert state.dispatch is not None

    available = _available_workers(worker, gemini_worker, openrouter_worker, shell_worker)
    worker_id, delivery_worker = _select_delivery_worker(state, available)
    if not delivery_worker:
        msg = f"Delivery failed: no suitable delivery worker configured (tried {worker_id})."
        logger.warning(msg)
        return _delivery_failure_response(
            state,
            msg,
            f"delivery failed (missing worker {worker_id})",
        )

    with start_optional_span(
        tracer_name="orchestrator.graph",
        span_name="orchestrator.node.deliver_result",
        attributes={"openinference.span.kind": SPAN_KIND_CHAIN},
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
    ):
        _log_delivery_start(state)

        gh_token = _delivery_github_token(state)
        if not gh_token and state.task_spec.delivery_mode == "draft_pr":
            msg = (
                "Delivery failed: GH_TOKEN or GITHUB_TOKEN not found in environment "
                "(required for PR creation)."
            )
            logger.warning(msg)
            return _delivery_failure_response(
                state,
                msg,
                "delivery failed (missing github token)",
            )

        branch_name, failure_response = _validated_delivery_branch(state)
        if failure_response is not None or branch_name is None:
            return failure_response or {"current_step": "deliver_result"}

        pr_title, pr_body = _delivery_pr_fields(state)
        prompt = _build_delivery_prompt(state, branch_name, pr_title, pr_body)
        request = _build_delivery_worker_request(
            state,
            prompt=prompt,
            gh_token=gh_token,
        )

        set_span_input_output(
            input_data={
                "delivery_mode": state.task_spec.delivery_mode,
                "branch": branch_name,
                "worker": worker_id,
            }
        )

        result = await _run_delivery_worker(state, delivery_worker, request)
        if isinstance(result, dict):
            return result

        return await _delivery_success_response(state, result, branch_name, pr_title, gh_token)


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
