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
from orchestrator.state import OrchestratorState, is_task_read_only
from sandbox import WorkspaceManager, WorkspaceMode, WorkspaceRequest
from workers.base import Worker, WorkerRequest

logger = logging.getLogger(__name__)


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
        if _makefile_has_target(repo_path / "Makefile", "setup"):
            return (
                "if command -v make >/dev/null 2>&1; then "
                "make setup; "
                "else echo 'make not installed; skipping Makefile setup'; fi"
            ), None
    return None, None


def _makefile_has_target(makefile_path: Any, target: str) -> bool:
    """Return true when a Makefile declares a concrete top-level target."""
    try:
        lines = makefile_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False

    target_prefix = f"{target}:"
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            continue
        if stripped.startswith(target_prefix):
            return True
    return False


async def _execute_gitignore_hardening(
    shell_worker: Worker,
    state: OrchestratorState,
    workspace_id: str,
) -> tuple[list[str], str | None]:
    if is_task_read_only(state):
        return [], "read_only_task"

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
        ".agent_home",
        ".code-agent",
    ]
    patterns_str = " ".join(noise_patterns)
    hardening_script = (
        "GIT_DIR=$(git rev-parse --git-dir 2>/dev/null); "
        'if [ -n "$GIT_DIR" ]; then '
        f"MISSING=''; for p in {patterns_str}; do "
        'if ! git check-ignore -q "$p" 2>/dev/null; then '
        'mkdir -p "$GIT_DIR/info"; '
        'if [ -f "$GIT_DIR/info/exclude" ] && '
        '[ -n "$(tail -c1 "$GIT_DIR/info/exclude" 2>/dev/null)" ]; '
        'then echo "" >> "$GIT_DIR/info/exclude"; fi; '
        'echo "$p" >> "$GIT_DIR/info/exclude"; MISSING="$MISSING $p"; fi; done; '
        'if [ -n "$MISSING" ]; then echo "hardened:$MISSING"; fi; '
        "fi"
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

    workspace_mode = state.task_spec.workspace_mode if state.task_spec else "clone"

    handle = workspace_manager.create_workspace(
        WorkspaceRequest(
            task_id=workspace_task_id,
            repo_url=state.task.repo_url or "",
            branch=state.task.branch,
            attempt=state.attempt_count + 1,
            workspace_mode=WorkspaceMode(workspace_mode),
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


def _task_spec_setup_commands(state: OrchestratorState) -> list[str]:
    if not state.task_spec:
        return []
    return [cmd.strip() for cmd in state.task_spec.setup_commands if cmd.strip()]


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

    setup_commands = _task_spec_setup_commands(state)
    error_msg = None

    if not setup_commands:
        heuristic_cmd, error_msg = _determine_setup_command(repo_path, allow_non_reproducible)
        if error_msg:
            return _init_fail(state, error_msg)
        if heuristic_cmd:
            setup_commands.append(heuristic_cmd)

    result = None
    setup_command = " && ".join(setup_commands) if setup_commands else None
    if setup_command:
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

    if not setup_command and not missing_ignores:
        return {"current_step": "init_environment", "result": None}

    return _build_init_response(
        state, setup_command, result, missing_ignores, hardening_skipped_reason
    )


def _build_init_response(
    state: OrchestratorState,
    setup_command: str | None,
    result: Any,
    missing_ignores: list[str],
    hardening_skipped_reason: str | None,
) -> dict[str, Any]:
    if setup_command:
        init_msg = f"Environment initialized successfully via: {setup_command}"
    else:
        init_msg = "Environment setup not required."

    if missing_ignores:
        hardening_msg = (
            f"Proactively hardened .git/info/exclude by adding: {', '.join(missing_ignores)}"
        )
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

    response: dict[str, Any] = {
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
    if state.dispatch.workspace_id:
        response["dispatch"] = {**state.dispatch.model_dump(), "workspace_id": None}
    return response
