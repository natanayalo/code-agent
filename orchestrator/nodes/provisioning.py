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
    if state.task.constraints.get("read_only") is True:
        return True

    profile_name = (state.route.chosen_profile or state.dispatch.worker_profile or "").lower()
    if "read-only" in profile_name or "read_only" in profile_name:
        return True

    if state.task_spec is None:
        return False

    if (
        state.task_spec.allowed_actions
        and "modify_workspace_files" not in state.task_spec.allowed_actions
    ):
        return True

    no_goal_text = " ".join(state.task_spec.non_goals).lower()
    if "do not modify any files" in no_goal_text:
        return True
    if "no files" in no_goal_text and "modified" in no_goal_text:
        return True
    if "do not create or modify" in no_goal_text:
        return True

    return False


def build_provision_workspace_node(
    workspace_manager: WorkspaceManager,
) -> Callable[[OrchestratorState], dict[str, Any]]:
    """Create a node that ensures a workspace is provisioned for the current attempt."""

    def provision_workspace(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)

        # If workspace_id is already set in the dispatch, we reuse it.
        # Note: We currently scope this to the task attempt in the graph transitions.
        if state.dispatch.workspace_id:
            return {"current_step": "provision_workspace"}

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

    return provision_workspace


def build_init_environment_node(
    workspace_manager: WorkspaceManager,
    shell_worker: Worker | None = None,
) -> Callable[[OrchestratorState], Awaitable[dict[str, Any]]]:
    """
    Create a node that initializes the environment (e.g. poetry install)
    in the shared workspace.
    """

    async def init_environment(state_input: OrchestratorState) -> dict[str, Any]:
        state = _ensure_state(state_input)

        if not shell_worker:
            logger.warning("init_environment skipped: shell_worker is not configured.")
            return {"current_step": "init_environment"}
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
        setup_command = None
        allow_non_reproducible = state.task.constraints.get("allow_non_reproducible_install", False)

        # Force re-init if the environment marker is missing
        env_marker_missing = False
        if (repo_path / "poetry.lock").exists() or (repo_path / "pyproject.toml").exists():
            if not (repo_path / ".venv").exists():
                env_marker_missing = True
        elif (repo_path / "package-lock.json").exists() or (repo_path / "yarn.lock").exists():
            if not (repo_path / "node_modules").exists():
                env_marker_missing = True

        if not env_marker_missing and state.task.constraints.get("skip_init_if_completed", True):
            # If we already have a success flag and markers exist, we can potentially skip.
            # (Note: Current graph always routes here, we rely on command-level idempotency).
            pass

        if (repo_path / "uv.lock").exists():
            setup_command = "command -v uv >/dev/null 2>&1 || pip install uv && uv sync"
        elif (repo_path / "poetry.lock").exists():
            setup_command = (
                "command -v poetry >/dev/null 2>&1 || pip install poetry && "
                "poetry config virtualenvs.in-project true --local && poetry install"
            )
        elif (repo_path / "requirements.txt").exists():
            if allow_non_reproducible:
                setup_command = "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
            else:
                return _init_fail(
                    state,
                    "Missing lockfile (poetry.lock or uv.lock) for deterministic Python install.",
                )
        elif (repo_path / "pnpm-lock.yaml").exists():
            setup_command = (
                "command -v pnpm >/dev/null 2>&1 || npm install -g pnpm && "
                "pnpm install --frozen-lockfile"
            )
        elif (repo_path / "yarn.lock").exists():
            setup_command = (
                "command -v yarn >/dev/null 2>&1 || npm install -g yarn && "
                "yarn install --frozen-lockfile"
            )
        elif (repo_path / "package-lock.json").exists():
            setup_command = "npm ci"
        elif (repo_path / "pyproject.toml").exists():
            if allow_non_reproducible:
                # Fallback to poetry if pyproject exists but no lockfile
                setup_command = (
                    "command -v poetry >/dev/null 2>&1 || pip install poetry && "
                    "poetry config virtualenvs.in-project true --local && poetry install"
                )
            else:
                return _init_fail(
                    state,
                    "Missing lockfile (poetry.lock or uv.lock) for deterministic Python install.",
                )
        elif (repo_path / "package.json").exists():
            if allow_non_reproducible:
                setup_command = "npm install"
            else:
                return _init_fail(
                    state,
                    (
                        "Missing lockfile (package-lock.json, yarn.lock, or pnpm-lock.yaml) "
                        "for deterministic Node install."
                    ),
                )
        elif (repo_path / "Cargo.toml").exists():
            setup_command = "cargo fetch"
        elif (repo_path / "go.mod").exists():
            setup_command = "go mod download"
        elif (repo_path / "Makefile").exists():
            setup_command = "make setup"

        if not setup_command:
            return {"current_step": "init_environment"}

        logger.info(
            "Initializing environment in shared workspace",
            extra={
                "workspace_id": workspace_id,
                "command": setup_command,
            },
        )

        # Run the setup via shell_worker
        # T-182: Ensure poetry knows about the local venv in the init container.
        init_secrets = dict(state.task.secrets)
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
        result = await shell_worker.run(request)

        # Propagate execution details to the trace for better visibility (T-180 parity)
        if result.stdout:
            set_current_span_attribute(NATIVE_AGENT_STDOUT_ATTRIBUTE, result.stdout)
        if result.stderr:
            set_current_span_attribute(NATIVE_AGENT_STDERR_ATTRIBUTE, result.stderr)
        set_span_input_output(input_data=None, output_data=result.summary)

        if result.status != "success":
            return {
                "current_step": "init_environment",
                "result": result.model_dump(),
                "progress_updates": _progress_update(state, "environment initialization failed"),
                **_timeline_event(
                    state,
                    TimelineEventType.INFRA_FAILURE,
                    message=f"Environment setup failed: {result.summary}",
                    payload={"status": result.status, "summary": result.summary},
                ),
            }

        # --- Gitignore Self-Healing (T-200) ---
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
        # Consolidate checks into a single command to reduce container overhead
        hardening_script = (
            f"MISSING=''; for p in {patterns_str}; do "
            'if ! git check-ignore -q "$p"; then '
            'echo "$p" >> .gitignore; MISSING="$MISSING $p"; fi; done; '
            'if [ -n "$MISSING" ]; then echo "hardened:$MISSING"; fi'
        )

        missing_ignores = []
        hardening_skipped_reason = None
        if _should_skip_gitignore_hardening(state):
            hardening_skipped_reason = _READ_ONLY_HARDENING_SKIP_REASON
        else:
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

            if hardening_result.stdout and "hardened:" in hardening_result.stdout:
                # Extract the list of patterns that were added
                raw_list = hardening_result.stdout.split("hardened:")[1].strip()
                missing_ignores = [p.strip() for p in raw_list.split(" ") if p.strip()]

        init_msg = f"Environment initialized successfully via: {setup_command}"
        extra_events = []
        if missing_ignores:
            hardening_msg = (
                f"Proactively hardened .gitignore by adding: {', '.join(missing_ignores)}"
            )
            logger.info(f"Task {state.task.task_id}: {hardening_msg}")
            init_msg += f" ({hardening_msg})"
            # We also add a specific event for the hardening action
            extra_events.append(
                _timeline_event(
                    state,
                    TimelineEventType.ENVIRONMENT_INITIALIZED,  # Reuse or just stick to one
                    message=hardening_msg,
                    payload={"hardened_patterns": missing_ignores},
                )
            )

        response = {
            "current_step": "init_environment",
            "result": result.model_dump(),
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

        # Merge extra events if we want them separate, but usually one clean event is better.
        # I'll stick to the single consolidated response for clarity.
        return response

    return init_environment


def _init_fail(state: OrchestratorState, message: str) -> dict[str, Any]:
    """Return a hard-failure result for environment initialization."""
    from workers.base import WorkerResult

    return {
        "current_step": "init_environment",
        "result": WorkerResult(
            status="error",
            summary=message,
            failure_kind="sandbox_infra",
        ).model_dump(),
        "progress_updates": _progress_update(state, "environment initialization aborted"),
        **_timeline_event(
            state,
            TimelineEventType.INFRA_FAILURE,
            message=message,
        ),
    }
