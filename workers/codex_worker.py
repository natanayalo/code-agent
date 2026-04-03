"""Codex worker adapter backed by sandboxed toy repo execution."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shlex
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from sandbox import (
    DockerSandboxCommand,
    DockerSandboxResult,
    DockerSandboxRunner,
    DockerSandboxRunnerError,
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceRequest,
)
from sandbox.workspace import _mask_url_credentials
from workers.base import (
    ArtifactReference,
    TestResult,
    Worker,
    WorkerCommand,
    WorkerRequest,
    WorkerResult,
)

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ROOT_ENV_VAR = "CODE_AGENT_WORKSPACE_ROOT"
DEFAULT_SANDBOX_TIMEOUT_SECONDS = 300
_TOY_TASK_SCRIPT_CONTAINER_PATH = "/workspace/.code-agent/codex_worker_task.py"
_TOY_TASK_SCRIPT_RELATIVE_PATH = Path(".code-agent") / "codex_worker_task.py"
_TOY_TASK_CONTEXT_CONTAINER_PATH = "/workspace/.code-agent/codex_worker_context.json"
_TOY_TASK_CONTEXT_RELATIVE_PATH = Path(".code-agent") / "codex_worker_context.json"
_TOY_TASK_TEMPLATE_SOURCE_PATH = Path(__file__).with_name("codex_worker_task_template.py")


def _slugify(value: str) -> str:
    """Create a filesystem-safe slug for sandbox bookkeeping."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _default_workspace_root() -> Path:
    """Return the default workspace root, honoring an environment override."""
    configured_root = os.environ.get(DEFAULT_WORKSPACE_ROOT_ENV_VAR)
    if configured_root:
        return Path(configured_root).expanduser()
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        workspace_owner = f"uid-{getuid()}"
    else:
        username = os.environ.get("USER") or os.environ.get("USERNAME")
        if username:
            workspace_owner = f"user-{_slugify(username)}"
        else:
            workspace_owner = f"pid-{os.getpid()}"
    return Path(tempfile.gettempdir()) / f"code-agent-workspaces-{workspace_owner}"


def _workspace_task_id(request: WorkerRequest) -> str:
    """Build a readable workspace task identifier from the worker request."""
    source = request.session_id or request.task_text
    return f"codex-{_slugify(source)}"


def _build_execution_context(request: WorkerRequest) -> dict[str, object]:
    """Build the inspectable worker execution context persisted in the workspace."""
    return {
        "task_text": request.task_text,
        "session_id": request.session_id or "unknown",
        "repo_url": _mask_url_credentials(request.repo_url or ""),
        "branch": request.branch or "default",
        "memory_context": request.memory_context,
        "constraints": request.constraints,
        "budget": request.budget,
    }


def _jsonify_execution_context_value(value: object, *, field_path: str) -> object:
    """Normalize context values into JSON-safe structures without hiding bad types."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes | bytearray):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return f"base64:{encoded}"
    if isinstance(value, Path | date | datetime | time):
        return str(value)
    if isinstance(value, Enum):
        return _jsonify_execution_context_value(value.value, field_path=field_path)
    if isinstance(value, BaseModel):
        return _jsonify_execution_context_value(
            value.model_dump(mode="json"),
            field_path=field_path,
        )
    if isinstance(value, Mapping):
        return {
            str(key): _jsonify_execution_context_value(
                item,
                field_path=f"{field_path}.{key}",
            )
            for key, item in value.items()
        }
    if isinstance(value, set | frozenset):
        normalized_items = [
            _jsonify_execution_context_value(item, field_path=f"{field_path}[{index}]")
            for index, item in enumerate(value)
        ]
        return sorted(
            normalized_items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            _jsonify_execution_context_value(item, field_path=f"{field_path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{field_path} contains unsupported value of type {type(value).__name__}.")


def _build_test_result_details(
    *,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> str | None:
    """Build a compact test-result detail message from sandbox output."""
    normalized_stdout = stdout.strip()
    normalized_stderr = stderr.strip()
    if exit_code == 0:
        return normalized_stdout or normalized_stderr or None

    sections: list[str] = []
    if normalized_stderr:
        sections.append(f"STDERR:\n{normalized_stderr}")
    if normalized_stdout:
        sections.append(f"STDOUT:\n{normalized_stdout}")
    return "\n\n".join(sections) or None


def _apply_cleanup_outcome(
    result: WorkerResult,
    *,
    workspace_deleted: bool,
) -> WorkerResult:
    """Keep the reported result consistent with the final workspace state."""
    if not workspace_deleted:
        return result

    if result.status == "success":
        summary = "CodexWorker completed a sandboxed toy repo task and cleaned up the workspace."
    elif result.status == "failure":
        base_summary = result.summary or "CodexWorker toy repo task failed"
        summary = f"{base_summary.rstrip('.')} and cleaned up the workspace."
    elif result.summary:
        summary = f"{result.summary} Workspace cleaned up per policy."
    else:
        summary = "CodexWorker cleaned up the workspace per policy."

    return result.model_copy(
        update={
            "summary": summary,
            "artifacts": [],
            "next_action_hint": None,
        }
    )


def _workspace_artifacts(
    workspace: WorkspaceHandle,
    *,
    sandbox_artifacts: list[ArtifactReference] | None = None,
) -> list[ArtifactReference]:
    """Build artifact references for a retained workspace."""
    artifacts = [
        ArtifactReference(
            name="workspace",
            uri=str(workspace.workspace_path),
            artifact_type="workspace",
        )
    ]
    if sandbox_artifacts:
        artifacts.extend(sandbox_artifacts)
    return artifacts


def _load_toy_task_script_source() -> str:
    """Read the packaged toy task script source that gets copied into workspaces."""
    return _TOY_TASK_TEMPLATE_SOURCE_PATH.read_text(encoding="utf-8")


def _write_toy_task_script(workspace: WorkspaceHandle) -> Path:
    """Persist the toy task script under the mounted workspace root."""
    script_path = workspace.workspace_path / _TOY_TASK_SCRIPT_RELATIVE_PATH
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_load_toy_task_script_source(), encoding="utf-8")
    return script_path


def _write_execution_context_file(
    workspace: WorkspaceHandle,
    request: WorkerRequest,
) -> Path:
    """Persist the worker execution context under the mounted workspace root."""
    context_path = workspace.workspace_path / _TOY_TASK_CONTEXT_RELATIVE_PATH
    context_path.parent.mkdir(parents=True, exist_ok=True)
    execution_context = _build_execution_context(request)
    serialized_context = {
        key: _jsonify_execution_context_value(value, field_path=key)
        for key, value in execution_context.items()
    }
    context_path.write_text(
        json.dumps(
            serialized_context,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return context_path


def _worker_result_from_sandbox_result(
    workspace: WorkspaceHandle,
    sandbox_result: DockerSandboxResult,
) -> tuple[WorkerResult, bool]:
    """Map sandbox output into the shared worker result contract."""
    command_summary = shlex.join(sandbox_result.command)
    sandbox_artifacts = [
        ArtifactReference(
            name=artifact.name,
            uri=artifact.uri,
            artifact_type=artifact.artifact_type,
        )
        for artifact in sandbox_result.artifacts
    ]
    status: Literal["success", "failure"] = (
        "success" if sandbox_result.exit_code == 0 else "failure"
    )
    result = WorkerResult(
        status=status,
        summary=(
            "CodexWorker completed a sandboxed toy repo task and retained the workspace."
            if status == "success"
            else f"CodexWorker toy repo task exited with code {sandbox_result.exit_code}."
        ),
        commands_run=[
            WorkerCommand(
                command=command_summary,
                exit_code=sandbox_result.exit_code,
                duration_seconds=sandbox_result.duration_seconds,
            )
        ],
        files_changed=sandbox_result.files_changed,
        test_results=[
            TestResult(
                name="codex_worker_toy_task",
                status="passed" if sandbox_result.exit_code == 0 else "failed",
                details=_build_test_result_details(
                    exit_code=sandbox_result.exit_code,
                    stdout=sandbox_result.stdout,
                    stderr=sandbox_result.stderr,
                ),
            )
        ],
        artifacts=_workspace_artifacts(workspace, sandbox_artifacts=sandbox_artifacts),
        next_action_hint="inspect_workspace_artifacts",
    )
    return result, status == "success"


class CodexWorker(Worker):
    """Execute a deterministic toy repo task through the sandbox stack."""

    def __init__(
        self,
        *,
        workspace_manager: WorkspaceManager | None = None,
        sandbox_runner: DockerSandboxRunner | None = None,
        workspace_root: str | Path | None = None,
        cleanup_policy: WorkspaceCleanupPolicy | None = None,
        timeout_seconds: int = DEFAULT_SANDBOX_TIMEOUT_SECONDS,
    ) -> None:
        self.cleanup_policy = cleanup_policy or WorkspaceCleanupPolicy(
            delete_on_success=False,
            retain_on_failure=True,
        )
        self.workspace_manager = workspace_manager or WorkspaceManager(
            workspace_root or _default_workspace_root(),
            cleanup_policy=self.cleanup_policy,
        )
        self.sandbox_runner = sandbox_runner or DockerSandboxRunner()
        self.timeout_seconds = timeout_seconds

    async def run(self, request: WorkerRequest) -> WorkerResult:
        """Provision a workspace, run the toy task, and return a typed result."""
        return await asyncio.to_thread(self._run_sync, request)

    def _execute_task_in_workspace(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        *,
        workspace_task_id: str,
    ) -> tuple[WorkerResult, bool]:
        """Write sandbox inputs, run the toy task, and map the result."""
        try:
            _write_toy_task_script(workspace)
            _write_execution_context_file(workspace, request)
        except TypeError as exc:
            logger.exception(
                "Codex worker failed to serialize sandbox execution context",
                extra={
                    "session_id": request.session_id,
                    "workspace_id": workspace.workspace_id,
                    "workspace_task_id": workspace_task_id,
                },
            )
            return (
                WorkerResult(
                    status="error",
                    summary=f"CodexWorker failed to serialize the sandbox execution context: {exc}",
                    artifacts=_workspace_artifacts(workspace),
                    next_action_hint="inspect_worker_configuration",
                ),
                False,
            )
        except OSError as exc:
            logger.exception(
                "Codex worker failed to prepare sandbox task files",
                extra={
                    "session_id": request.session_id,
                    "workspace_id": workspace.workspace_id,
                    "workspace_task_id": workspace_task_id,
                },
            )
            return (
                WorkerResult(
                    status="error",
                    summary=f"CodexWorker failed to prepare the sandbox task files: {exc}",
                    artifacts=_workspace_artifacts(workspace),
                    next_action_hint="inspect_workspace_artifacts",
                ),
                False,
            )

        try:
            sandbox_result = self.sandbox_runner.run(
                DockerSandboxCommand(
                    workspace=workspace,
                    command=[
                        "python3",
                        _TOY_TASK_SCRIPT_CONTAINER_PATH,
                        _TOY_TASK_CONTEXT_CONTAINER_PATH,
                    ],
                    timeout_seconds=self.timeout_seconds,
                )
            )
        except DockerSandboxRunnerError as exc:
            logger.exception(
                "Codex worker sandbox execution failed",
                extra={
                    "session_id": request.session_id,
                    "workspace_id": workspace.workspace_id,
                    "workspace_task_id": workspace_task_id,
                },
            )
            return (
                WorkerResult(
                    status="error",
                    summary=f"CodexWorker sandbox execution failed: {exc}",
                    artifacts=_workspace_artifacts(workspace),
                    next_action_hint="inspect_workspace_artifacts",
                ),
                False,
            )

        result, run_succeeded = _worker_result_from_sandbox_result(workspace, sandbox_result)
        logger.info(
            "Codex worker finished",
            extra={
                "session_id": request.session_id,
                "workspace_id": workspace.workspace_id,
                "status": result.status,
                "files_changed_count": len(result.files_changed),
                "artifact_count": len(result.artifacts),
            },
        )
        return result, run_succeeded

    def _cleanup_workspace(
        self,
        request: WorkerRequest,
        workspace: WorkspaceHandle,
        *,
        workspace_task_id: str,
        run_succeeded: bool,
    ) -> bool:
        """Apply the workspace cleanup policy and swallow cleanup errors."""
        try:
            return self.workspace_manager.cleanup_workspace(
                workspace,
                succeeded=run_succeeded,
            )
        except WorkspaceManagerError:
            logger.exception(
                "Codex worker failed to clean up workspace",
                extra={
                    "session_id": request.session_id,
                    "workspace_id": workspace.workspace_id,
                    "workspace_task_id": workspace_task_id,
                    "run_succeeded": run_succeeded,
                },
            )
            return False

    def _run_sync(self, request: WorkerRequest) -> WorkerResult:
        """Provision a workspace, run the toy task, and return a typed result."""
        if request.repo_url is None or not request.repo_url.strip():
            return WorkerResult(
                status="error",
                summary=(
                    "CodexWorker requires a non-empty repo_url " "to provision a sandbox workspace."
                ),
                next_action_hint="provide_repo_url",
            )

        workspace_task_id = _workspace_task_id(request)
        logger.info(
            "Starting Codex worker run",
            extra={
                "session_id": request.session_id,
                "repo_url": _mask_url_credentials(request.repo_url),
                "branch": request.branch,
                "workspace_task_id": workspace_task_id,
            },
        )

        try:
            workspace = self.workspace_manager.create_workspace(
                WorkspaceRequest(
                    task_id=workspace_task_id,
                    repo_url=request.repo_url,
                    branch=request.branch,
                    cleanup_policy=self.cleanup_policy,
                )
            )
        except (WorkspaceManagerError, OSError) as exc:
            logger.exception(
                "Codex worker failed to provision workspace",
                extra={"session_id": request.session_id, "workspace_task_id": workspace_task_id},
            )
            return WorkerResult(
                status="error",
                summary=f"CodexWorker failed to provision a workspace: {exc}",
                next_action_hint="inspect_worker_configuration",
            )

        result: WorkerResult | None = None
        run_succeeded = False
        try:
            result, run_succeeded = self._execute_task_in_workspace(
                request,
                workspace,
                workspace_task_id=workspace_task_id,
            )
        finally:
            workspace_deleted = self._cleanup_workspace(
                request,
                workspace,
                workspace_task_id=workspace_task_id,
                run_succeeded=run_succeeded,
            )

        if result is None:
            raise RuntimeError("Codex worker execution completed without a result.")

        if workspace_deleted:
            result = _apply_cleanup_outcome(
                result,
                workspace_deleted=workspace_deleted,
            )
            logger.info(
                "Codex worker cleanup policy deleted the workspace",
                extra={
                    "session_id": request.session_id,
                    "workspace_id": workspace.workspace_id,
                    "workspace_task_id": workspace_task_id,
                    "run_succeeded": run_succeeded,
                },
            )

        return result
