"""Nodes for workspace provisioning and environment initialization."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from apps.observability import (
    NATIVE_AGENT_STDERR_ATTRIBUTE,
    NATIVE_AGENT_STDOUT_ATTRIBUTE,
    OPENINFERENCE_SPAN_KIND_ATTRIBUTE,
    SPAN_KIND_TOOL,
    set_current_span_attribute,
    set_span_input_output,
)
from db.enums import TimelineEventType
from orchestrator.nodes.utils import _ensure_state, _progress_update, _timeline_event
from orchestrator.state import OrchestratorState
from sandbox import WorkspaceManager, WorkspaceRequest
from workers.base import Worker, WorkerRequest

logger = logging.getLogger(__name__)
_READ_ONLY_HARDENING_SKIP_REASON = "read_only_or_no_modification_task"


def _should_skip_gitignore_hardening(state: OrchestratorState) -> bool:
    """Return whether environment self-healing should avoid repo file edits."""
    if (state.task.constraints or {}).get("read_only") is True:
        return True

    route_profile = state.route.chosen_profile if state.route else None
    dispatch_profile = state.dispatch.worker_profile if state.dispatch else None
    profile_name = (route_profile or dispatch_profile or "").lower()
    if "read-only" in profile_name or "read_only" in profile_name:
        return True

    if state.task_spec is None:
        return False

    if (
        state.task_spec.allowed_actions
        and "modify_workspace_files" not in state.task_spec.allowed_actions
    ):
        return True

    no_goal_text = " ".join(state.task_spec.non_goals or []).lower()
    if "do not modify any files" in no_goal_text:
        return True
    if "no files" in no_goal_text and "modified" in no_goal_text:
        return True
    if "do not create or modify" in no_goal_text:
        return True

    return False


def _check_env_marker_missing(repo_path: Any) -> bool:
    if (
        (repo_path / "poetry.lock").exists()
        or (repo_path / "pyproject.toml").exists()
        or (repo_path / "uv.lock").exists()
        or (repo_path / "requirements.txt").exists()
    ) and not (repo_path / ".venv").exists():
        return True
    if (
        (repo_path / "package-lock.json").exists()
        or (repo_path / "yarn.lock").exists()
        or (repo_path / "pnpm-lock.yaml").exists()
        or (repo_path / "package.json").exists()
    ) and not (repo_path / "node_modules").exists():
        return True
    return False


def _determine_setup_command(
    repo_path: Any, allow_non_reproducible: bool
) -> tuple[str | None, str | None]:
    if (repo_path / "uv.lock").exists():
        return "command -v uv >/dev/null 2>&1 || pip install uv && uv sync", None
    elif (repo_path / "poetry.lock").exists():
        return (
            "command -v poetry >/dev/null 2>&1 || pip install poetry && "
            "poetry config virtualenvs.in-project true --local && poetry install"
        ), None
    elif (repo_path / "pnpm-lock.yaml").exists():
        return (
            "command -v pnpm >/dev/null 2>&1 || npm install -g pnpm && "
            "pnpm install --frozen-lockfile"
        ), None
    elif (repo_path / "yarn.lock").exists():
        return (
            "command -v yarn >/dev/null 2>&1 || npm install -g yarn && "
            "yarn install --frozen-lockfile"
        ), None
    elif (repo_path / "package-lock.json").exists():
        return "npm ci", None
    elif (repo_path / "Cargo.toml").exists():
        return "cargo fetch", None
    elif (repo_path / "go.mod").exists():
        return "go mod download", None
    elif (repo_path / "requirements.txt").exists():
        if allow_non_reproducible:
            return "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt", None
        else:
            return (
                None,
                "Missing lockfile (poetry.lock or uv.lock) for deterministic Python install.",
            )
    elif (repo_path / "pyproject.toml").exists():
        if allow_non_reproducible:
            return (
                "command -v poetry >/dev/null 2>&1 || pip install poetry && "
                "poetry config virtualenvs.in-project true --local && poetry install"
            ), None
        else:
            return (
                None,
                "Missing lockfile (poetry.lock or uv.lock) for deterministic Python install.",
            )
    elif (repo_path / "package.json").exists():
        if allow_non_reproducible:
            return "npm install", None
        else:
            return None, (
                "Missing lockfile (package-lock.json, yarn.lock, or pnpm-lock.yaml) "
                "for deterministic Node install."
            )
    elif (repo_path / "Makefile").exists():
        return "make setup", None
    return None, None


async def _execute_gitignore_hardening(
    shell_worker: Worker,
    state: OrchestratorState,
    workspace_id: str,
) -> tuple[list[str], str | None]:
    if _should_skip_gitignore_hardening(state):
        return [], _READ_ONLY_HARDENING_SKIP_REASON

    noise_patterns = [
        ".cache",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        "target",
        ".idea",
        ".vscode",
        ".DS_Store",
        ".env",
    ]
    patterns_str = " ".join(noise_patterns)
    hardening_script = (
        f"MISSING=''; for p in {patterns_str}; do "
        'if ! git check-ignore -q "$p" 2>/dev/null; then '
        'if [ -f .gitignore ] && [ -n "$(tail -c1 .gitignore 2>/dev/null)" ]; '
        'then echo "" >> .gitignore; fi; '
        'echo "$p" >> .gitignore; MISSING="$MISSING $p"; fi; done; '
        'if [ -n "$MISSING" ]; then echo "hardened:$MISSING"; fi'
    )

    try:
        hardening_result = await shell_worker.run(
            WorkerRequest(
                session_id=state.session.session_id if state.session else None,
                task_id=state.task.task_id,
                repo_url=state.task.repo_url,
                branch=state.task.branch,
                workspace_id=workspace_id,
                task_text=hardening_script,
                budget={"worker_timeout_seconds": 15},
            )
        )

        if (
            hardening_result.status == "success"
            and hardening_result.stdout
            and "hardened:" in hardening_result.stdout
        ):
            import re

            for line in (hardening_result.stdout or "").splitlines():
                match = re.search(r"\bhardened:(.*)", line)
                if match:
                    raw_list = match.group(1).strip()
                    return raw_list.split(), None
    except RuntimeError as exc:
        logger.debug("Gitignore self-healing failed: %s", exc)
    return [], None


def _run_provision_workspace(
    state_input: OrchestratorState,
    workspace_manager: WorkspaceManager,
) -> dict[str, Any]:
    state = _ensure_state(state_input)

    # If workspace_id is already set in the dispatch, we reuse it.
    # Note: We currently scope this to the task attempt in the graph transitions.
    if state.dispatch.workspace_id:
        return {"current_step": "provision_workspace", "result": None}

    workspace_task_id = (
        state.task.task_id or f"task-{state.session.session_id if state.session else 'unknown'}"
    )

    logger.info(
        "Provisioning shared workspace for task attempt",
        extra={
            "task_id": workspace_task_id,
            "repo_url": state.task.repo_url,
            "attempt": state.attempt_count,
        },
    )

    handle = workspace_manager.create_workspace(
        WorkspaceRequest(
            task_id=workspace_task_id,
            repo_url=state.task.repo_url or "",
            branch=state.task.branch,
        )
    )

    return {
        "current_step": "provision_workspace",
        "result": None,
        "dispatch": {
            **state.dispatch.model_dump(),
            "workspace_id": handle.workspace_id,
        },
        "progress_updates": _progress_update(
            state, f"workspace provisioned: {handle.workspace_id}"
        ),
        **_timeline_event(
            state,
            TimelineEventType.WORKSPACE_PROVISIONED,
            message=f"Workspace '{handle.workspace_id}' created.",
            payload={"workspace_id": handle.workspace_id},
        ),
    }


def build_provision_workspace_node(
    workspace_manager: WorkspaceManager,
) -> Callable[[OrchestratorState], dict[str, Any]]:
    """Create a node that ensures a workspace is provisioned for the current attempt."""
    import functools

    return functools.partial(_run_provision_workspace, workspace_manager=workspace_manager)


async def _execute_setup_command(
    shell_worker: Worker,
    state: OrchestratorState,
    workspace_id: str,
    setup_command: str,
) -> Any:
    # Run the setup via shell_worker
    # T-182: Ensure poetry knows about the local venv in the init container.
    init_secrets = dict(state.task.secrets or {})
    init_secrets["POETRY_VIRTUALENVS_IN_PROJECT"] = "true"

    request = WorkerRequest(
        session_id=state.session.session_id if state.session else None,
        task_id=state.task.task_id,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        workspace_id=workspace_id,
        task_text=setup_command,
        budget={"worker_timeout_seconds": 600},  # 10 minutes for install
        network_enabled=True,
        secrets=init_secrets,
    )

    set_span_input_output(input_data=setup_command)
    set_current_span_attribute(OPENINFERENCE_SPAN_KIND_ATTRIBUTE, SPAN_KIND_TOOL)
    try:
        result = await shell_worker.run(request)
    except RuntimeError as exc:
        logger.debug("Shell worker execution failed: %s", exc)
        return _init_fail(
            state,
            f"Environment setup failed with exception: {type(exc).__name__}: {exc}",
        )

    # Propagate execution details to the trace for better visibility (T-180 parity)
    if hasattr(result, "stdout") and result.stdout:
        set_current_span_attribute(NATIVE_AGENT_STDOUT_ATTRIBUTE, result.stdout)
    if hasattr(result, "stderr") and result.stderr:
        set_current_span_attribute(NATIVE_AGENT_STDERR_ATTRIBUTE, result.stderr)
    set_span_input_output(input_data=None, output_data=getattr(result, "summary", ""))

    if getattr(result, "status", None) != "success":
        return {
            "current_step": "init_environment",
            "result": result,
            "progress_updates": _progress_update(state, "environment initialization failed"),
            **_timeline_event(
                state,
                TimelineEventType.INFRA_FAILURE,
                message=f"Environment setup failed: {getattr(result, 'summary', '')}",
                payload={
                    "status": getattr(result, "status", ""),
                    "summary": getattr(result, "summary", ""),
                },
            ),
        }
    return result


async def _run_init_environment(
    state_input: OrchestratorState,
    workspace_manager: WorkspaceManager,
    shell_worker: Worker | None = None,
) -> dict[str, Any]:
    state = _ensure_state(state_input)

    if not shell_worker:
        logger.warning("init_environment skipped: shell_worker is not configured.")
        return {"current_step": "init_environment", "result": None}
    workspace_id = state.dispatch.workspace_id
    if not workspace_id:
        raise RuntimeError("init_environment called before provision_workspace")

    # Reuse existing workspace handle
    handle = workspace_manager.get_workspace(
        workspace_id,
        repo_url=state.task.repo_url,
        branch=state.task.branch,
        task_id=state.task.task_id,
    )

    repo_path = handle.repo_path

    # Selection Policy (T-178)
    allow_non_reproducible = (state.task.constraints or {}).get(
        "allow_non_reproducible_install", False
    )
    env_marker_missing = _check_env_marker_missing(repo_path)

    has_success_flag = any(
        (e.event_type.value if hasattr(e.event_type, "value") else e.event_type)
        == TimelineEventType.ENVIRONMENT_INITIALIZED.value
        for e in state.timeline_events
    )

    if (
        not env_marker_missing
        and has_success_flag
        and (state.task.constraints or {}).get("skip_init_if_completed", True)
    ):
        return {"current_step": "init_environment", "result": None}

    setup_command, error_msg = _determine_setup_command(repo_path, allow_non_reproducible)
    if error_msg:
        return _init_fail(state, error_msg)

    if not setup_command:
        return {"current_step": "init_environment", "result": None}

    logger.info(
        "Initializing environment in shared workspace",
        extra={
            "workspace_id": workspace_id,
            "command": setup_command,
        },
    )

    result = await _execute_setup_command(shell_worker, state, workspace_id, setup_command)
    if isinstance(result, dict) and result.get("current_step"):
        return result

    missing_ignores, hardening_skipped_reason = await _execute_gitignore_hardening(
        shell_worker, state, workspace_id
    )

    return _build_init_response(
        state, setup_command, result, missing_ignores, hardening_skipped_reason
    )


def _build_init_response(
    state: OrchestratorState,
    setup_command: str,
    result: Any,
    missing_ignores: list[str],
    hardening_skipped_reason: str | None,
) -> dict[str, Any]:
    init_msg = f"Environment initialized successfully via: {setup_command}"
    if missing_ignores:
        hardening_msg = f"Proactively hardened .gitignore by adding: {', '.join(missing_ignores)}"
        logger.info(f"Task {state.task.task_id}: {hardening_msg}")
        init_msg += f" ({hardening_msg})"

    return {
        "current_step": "init_environment",
        "result": result,
        "progress_updates": _progress_update(state, "environment initialized"),
        **_timeline_event(
            state,
            TimelineEventType.ENVIRONMENT_INITIALIZED,
            message=init_msg,
            payload={
                "command": setup_command,
                "hardened_ignores": missing_ignores,
                "hardening_skipped_reason": hardening_skipped_reason,
            },
        ),
    }


def build_init_environment_node(
    workspace_manager: WorkspaceManager,
    shell_worker: Worker | None = None,
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """
    Create a node that initializes the environment (e.g. poetry install)
    in the shared workspace.
    """
    import functools

    return functools.partial(
        _run_init_environment,
        workspace_manager=workspace_manager,
        shell_worker=shell_worker,
    )


def _init_fail(state: OrchestratorState, message: str) -> dict[str, Any]:
    """Return a hard-failure result for environment initialization."""
    from workers.base import WorkerResult

    return {
        "current_step": "init_environment",
        "result": WorkerResult(
            status="error",
            summary=message,
            failure_kind="sandbox_infra",
        ),
        "progress_updates": _progress_update(state, "environment initialization aborted"),
        **_timeline_event(
            state,
            TimelineEventType.INFRA_FAILURE,
            message=message,
        ),
    }
