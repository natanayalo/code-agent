"""Delivery node implementation for GitHub branch and PR integration."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any

from apps.observability import (
    SPAN_KIND_CHAIN,
    set_span_input_output,
    start_optional_span,
)
from db.enums import TimelineEventType, WorkerRunStatus
from orchestrator.acceptance import verification_rejection
from orchestrator.github_delivery import (
    capture_delivery_metadata,
    publish_draft_pr_from_workspace,
)
from orchestrator.nodes.utils import (
    _ensure_state,
    _progress_update,
    _timeline_event,
)
from orchestrator.state import OrchestratorState
from sandbox.workspace import build_authenticated_github_git_env
from workers.base import FailureKind, WorkerResult

logger = logging.getLogger(__name__)

_BROKER_GIT_AUTHOR_NAME = "Code Agent"
_BROKER_GIT_AUTHOR_EMAIL = "code-agent@localhost"
_DELIVERY_DIAGNOSTIC_FILENAMES = frozenset(
    {"coverage_report.txt", "run_test_select.py", "run_test_selection.py"}
)
_DELIVERY_RUNTIME_DIRECTORIES = frozenset(
    {
        ".agent_home",
        ".cache",
        ".code-agent",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "target",
    }
)


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


def _delivery_failure_response(
    state: OrchestratorState,
    msg: str,
    progress_message: str,
    *,
    payload: dict[str, Any] | None = None,
    failure_kind: FailureKind = "incomplete_delivery",
) -> dict[str, Any]:
    prior_result = state.result
    if prior_result is not None:
        summary_parts = [prior_result.summary] if prior_result.summary else []
        summary_parts.append(f"Delivery Output:\n{msg}")
        failure_result = prior_result.model_copy(
            update={
                "status": "failure",
                "failure_kind": failure_kind,
                "summary": "\n\n".join(summary_parts),
            }
        )
    else:
        failure_result = WorkerResult(status="failure", summary=msg, failure_kind=failure_kind)
    return {
        "current_step": "deliver_result",
        "progress_updates": _progress_update(state, progress_message),
        "result": failure_result,
        **_timeline_event(
            state,
            TimelineEventType.DELIVERY_FAILED,
            message=msg,
            payload={**(payload or {}), "failure_kind": failure_kind},
        ),
    }


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


def _draft_pr_token_failure(
    state: OrchestratorState, gh_token: str | None
) -> dict[str, Any] | None:
    assert state.task_spec is not None
    if gh_token or state.task_spec.delivery_mode != "draft_pr":
        return None
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


def _capture_delivery_metadata(
    state: OrchestratorState,
    branch_name: str,
    gh_token: str | None,
) -> dict[str, Any] | None:
    if not state.task_spec:
        return None
    return capture_delivery_metadata(
        repo_url=state.task.repo_url,
        delivery_mode=state.task_spec.delivery_mode,
        branch_name=branch_name,
        token=gh_token,
    )


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
    pr_body: str,
    gh_token: str | None,
    trusted_git_dir: Path | None = None,
) -> dict[str, Any]:
    merged_result = _merge_delivery_result(state.result, delivery_result)
    delivery_metadata = await asyncio.to_thread(
        _capture_delivery_metadata, state, branch_name, gh_token
    )
    if (
        state.task_spec
        and state.task_spec.delivery_mode == "draft_pr"
        and gh_token
        and not (delivery_metadata or {}).get("pr_url")
    ):
        assert state.dispatch is not None
        delivery_metadata = await asyncio.to_thread(
            publish_draft_pr_from_workspace,
            repo_url=state.task.repo_url,
            workspace_id=state.dispatch.workspace_id or "",
            branch_name=branch_name,
            base_branch=state.task_spec.target_branch or state.task.branch or "master",
            pr_title=pr_title,
            pr_body=pr_body,
            token=gh_token,
            trusted_git_dir=trusted_git_dir,
        )
    if (
        state.task_spec
        and state.task_spec.delivery_mode == "draft_pr"
        and not (delivery_metadata or {}).get("pr_url")
    ):
        return _delivery_failure_response(
            state,
            "Delivery failed: GitHub did not confirm the requested draft PR.",
            "delivery failed (draft PR not confirmed)",
            payload={"branch": branch_name},
        )
    if delivery_metadata:
        merged_result.delivery_metadata = delivery_metadata
    return _delivery_completed_response(
        state,
        branch_name=branch_name,
        pr_title=pr_title,
        merged_result=merged_result,
    )


async def _reconcile_existing_draft_pr(
    state: OrchestratorState,
    *,
    branch_name: str,
    pr_title: str,
    gh_token: str | None,
) -> dict[str, Any] | None:
    if not state.task_spec or state.task_spec.delivery_mode != "draft_pr":
        return None

    delivery_metadata = await asyncio.to_thread(
        _capture_delivery_metadata, state, branch_name, gh_token
    )
    if not delivery_metadata or not delivery_metadata.get("pr_url"):
        return None

    delivery_result = WorkerResult(
        status="success",
        summary="Existing draft PR reconciled.",
        delivery_metadata=delivery_metadata,
    )
    merged_result = _merge_delivery_result(state.result, delivery_result)
    merged_result.delivery_metadata = delivery_metadata
    return _delivery_completed_response(
        state,
        branch_name=branch_name,
        pr_title=pr_title,
        merged_result=merged_result,
    )


def _resolve_broker_github_token(
    state: OrchestratorState,
    session_factory: Any | None,
) -> str | None:
    """Resolve the task's broker-only GitHub token without sandbox exposure."""
    token = _delivery_github_token(state)
    if token or session_factory is None:
        return token

    from sandbox.ephemeral_store_postgres import SessionFactoryEphemeralSecretStore
    from sandbox.secrets import SecretExposurePolicy, SecretRegistry, SecretScope

    store = SessionFactoryEphemeralSecretStore(session_factory)
    registry = SecretRegistry(ephemeral_store=store, task_id=state.task.task_id)
    for ref in state.task.secret_refs or ():
        try:
            definition = registry.get(ref.name)
            if (
                definition is None
                or definition.exposure_policy != SecretExposurePolicy.BROKER_ONLY
                or definition.required_scope != SecretScope.GIT_PUSH
            ):
                continue
            token = store.get(ref.name, task_id=state.task.task_id)
        except Exception:
            logger.debug("Failed to resolve broker-only delivery credential", exc_info=True)
            continue
        if token:
            return token
    return None


def _delivery_files_to_stage(state: OrchestratorState) -> tuple[list[str], str | None]:
    """Return the worker-reported workspace paths safe for broker staging."""
    if state.result is None:
        return [], None

    paths: list[str] = []
    for raw_path in state.result.files_changed:
        path = PurePosixPath(raw_path)
        if (
            raw_path.startswith("/")
            or not path.parts
            or any(part in {".", ".."} for part in path.parts)
        ):
            return [], f"Delivery failed: worker reported unsafe path {raw_path!r}."
        if (
            path.name in _DELIVERY_DIAGNOSTIC_FILENAMES
            or path.parts[0] in _DELIVERY_RUNTIME_DIRECTORIES
        ):
            logger.info("Skipping runtime or diagnostic delivery path", extra={"path": raw_path})
            continue
        if raw_path not in paths:
            paths.append(raw_path)
    return paths, None


def _broker_git_environment(gh_token: str | None) -> dict[str, str]:
    """Build the broker-only environment needed for an authenticated GitHub push."""
    return build_authenticated_github_git_env(gh_token)


def _run_broker_git_commands(
    *,
    trusted_git_dir: Path,
    workspace_path: Path,
    files_to_stage: list[str],
    gh_token: str | None,
    pr_title: str,
    pr_body: str,
    branch_name: str,
) -> tuple[str | None, str | None]:
    """Run the broker-owned Git sequence outside the Temporal event loop."""

    def _run_git(
        *args: str, include_github_token: bool = False
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                f"--git-dir={trusted_git_dir}",
                f"--work-tree={workspace_path}",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env=_broker_git_environment(gh_token) if include_github_token else None,
        )

    reset_res = _run_git("reset")
    if reset_res.returncode != 0:
        return (
            f"Delivery failed to reset staging area: {reset_res.stderr}",
            "delivery failed (git reset)",
        )

    if files_to_stage:
        add_res = _run_git("add", "--", *files_to_stage)
        if add_res.returncode != 0:
            return f"Delivery failed to stage files: {add_res.stderr}", "delivery failed (git add)"

    staged_res = _run_git("diff", "--cached", "--quiet")
    if staged_res.returncode == 1:
        commit_res = _run_git(
            "-c",
            f"user.name={_BROKER_GIT_AUTHOR_NAME}",
            "-c",
            f"user.email={_BROKER_GIT_AUTHOR_EMAIL}",
            "commit",
            "-m",
            f"{pr_title}\n\n{pr_body}",
        )
        if commit_res.returncode != 0:
            return f"Delivery failed to commit: {commit_res.stderr}", "delivery failed (git commit)"
    elif staged_res.returncode != 0:
        return (
            f"Delivery failed to inspect staged files: {staged_res.stderr}",
            "delivery failed (git diff --cached)",
        )

    push_res = _run_git(
        "push",
        "-u",
        "origin",
        f"HEAD:refs/heads/{branch_name}",
        include_github_token=True,
    )
    if push_res.returncode != 0:
        return f"Delivery failed to push branch: {push_res.stderr}", "delivery failed (git push)"

    return None, None


async def _run_deliver_result(
    state_input: OrchestratorState,
    session_factory: Any | None = None,
) -> dict[str, Any]:
    state = _ensure_state(state_input)

    if state.result and state.result.status == "success":
        if rejection := verification_rejection(state):
            kind, message = rejection
            return _delivery_failure_response(
                state, message, "acceptance failed (verification)", failure_kind=kind
            )
        if (
            state.task_spec
            and state.task_spec.delivery_mode in {"branch", "draft_pr"}
            and not state.dispatch.workspace_id
        ):
            return _delivery_failure_response(
                state,
                "Required delivery workspace is missing.",
                "delivery failed (missing workspace)",
            )
    if not _should_deliver_result(state):
        return {"current_step": "deliver_result"}
    assert state.task_spec is not None
    assert state.dispatch is not None

    with start_optional_span(
        tracer_name="orchestrator.graph",
        span_name="orchestrator.node.deliver_result",
        attributes={"openinference.span.kind": SPAN_KIND_CHAIN},
        task_id=state.task.task_id,
        session_id=state.session.session_id if state.session else None,
        attempt=state.attempt_count,
    ):
        _log_delivery_start(state)

        gh_token = _resolve_broker_github_token(state, session_factory)

        token_failure = _draft_pr_token_failure(state, gh_token)
        if token_failure is not None:
            return token_failure

        branch_name, failure_response = _validated_delivery_branch(state)
        if failure_response is not None or branch_name is None:
            return failure_response or {"current_step": "deliver_result"}

        pr_title, pr_body = _delivery_pr_fields(state)
        existing_pr_response = await _reconcile_existing_draft_pr(
            state,
            branch_name=branch_name,
            pr_title=pr_title,
            gh_token=gh_token,
        )
        if existing_pr_response is not None:
            return existing_pr_response

        set_span_input_output(
            input_data={
                "delivery_mode": state.task_spec.delivery_mode,
                "branch": branch_name,
                "worker": "broker",
            }
        )

        from sandbox.workspace import _trusted_git_dir, default_workspace_root

        workspace_id = state.dispatch.workspace_id
        assert workspace_id is not None
        workspace_root = default_workspace_root().resolve()
        workspace_path = (workspace_root / workspace_id).resolve()
        trusted_git_dir = _trusted_git_dir(workspace_path, workspace_id)

        if not workspace_path.exists() or not trusted_git_dir.exists():
            return _delivery_failure_response(
                state,
                "Delivery failed: workspace or trusted git dir missing.",
                "delivery failed (missing workspace)",
            )

        files_to_stage, path_failure = _delivery_files_to_stage(state)
        if path_failure is not None:
            return _delivery_failure_response(state, path_failure, "delivery failed (unsafe path)")
        failure_message, failure_progress_message = await asyncio.to_thread(
            _run_broker_git_commands,
            trusted_git_dir=trusted_git_dir,
            workspace_path=workspace_path,
            files_to_stage=files_to_stage,
            gh_token=gh_token,
            pr_title=pr_title,
            pr_body=pr_body,
            branch_name=branch_name,
        )
        if failure_message is not None:
            return _delivery_failure_response(
                state,
                failure_message,
                failure_progress_message or "delivery failed (broker git)",
            )

        delivery_result = WorkerResult(status="success", summary="Changes delivered via broker.")

        return await _delivery_success_response(
            state,
            delivery_result,
            branch_name,
            pr_title,
            pr_body,
            gh_token,
            trusted_git_dir=trusted_git_dir,
        )


def build_deliver_result_node(
    session_factory: Any | None = None,
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """Factory for the delivery node."""
    import functools

    return functools.partial(
        _run_deliver_result,
        session_factory=session_factory,
    )
