"""Workspace management helpers for sandboxed task execution."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from sandbox.constants import DEFAULT_SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS
from sandbox.redact import mask_url_credentials as _mask_url_credentials

logger = logging.getLogger(__name__)


class CommandRunner(Protocol):
    """Protocol for running external commands."""

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = DEFAULT_SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS,
    ) -> None: ...


class SandboxModel(BaseModel):
    """Base model for sandbox-related data structures."""

    model_config = ConfigDict(extra="forbid")


class SandboxArtifact(SandboxModel):
    """A persisted artifact produced by a sandbox command run."""

    name: str
    uri: str
    artifact_type: str | None = None
    artifact_metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceCleanupPolicy(SandboxModel):
    """Cleanup rules for a task workspace."""

    delete_on_success: bool = True
    retain_on_failure: bool = True


DEFAULT_WORKSPACE_ROOT_ENV_VAR = "CODE_AGENT_WORKSPACE_ROOT"


class WorkspaceMode(str, Enum):
    """How the workspace should be initialized."""

    CLONE = "clone"
    INIT = "init"
    NONE = "none"


class WorkspaceRequest(SandboxModel):
    """Input required to provision a task workspace."""

    task_id: str = Field(min_length=1)
    repo_url: str = Field(default="")
    branch: str | None = None
    workspace_mode: WorkspaceMode = WorkspaceMode.CLONE
    attempt: int = 1
    cleanup_policy: WorkspaceCleanupPolicy | None = None


class WorkspaceHandle(SandboxModel):
    """Details for a provisioned task workspace."""

    workspace_id: str
    task_id: str
    workspace_path: Path
    repo_path: Path
    repo_url: str
    branch: str | None = None
    workspace_mode: WorkspaceMode = WorkspaceMode.CLONE
    cleanup_policy: WorkspaceCleanupPolicy


class WorkspaceManagerError(RuntimeError):
    """Raised when workspace provisioning or cleanup fails."""


def _slugify_task_id(task_id: str) -> str:
    """Normalize a task id for filesystem-safe workspace naming."""
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:64]
    return slug or "task"


def _slugify_workspace_owner(value: str) -> str:
    """Normalize a workspace owner component for the default path."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "task"


def _build_workspace_id(task_id: str, attempt: int = 1) -> str:
    """Generate a readable deterministic workspace identifier for a task and attempt."""
    import hashlib

    task_hash = hashlib.sha256(task_id.encode()).hexdigest()[:8]
    if attempt > 1:
        return f"workspace-{_slugify_task_id(task_id)}-{task_hash}-v{attempt}"
    return f"workspace-{_slugify_task_id(task_id)}-{task_hash}"


def default_workspace_root(env: Mapping[str, str] | None = None) -> Path:
    """Return the default workspace root, honoring an environment override."""
    environ = env if env is not None else os.environ
    configured_root = environ.get(DEFAULT_WORKSPACE_ROOT_ENV_VAR)
    configured_root = configured_root.strip() if configured_root is not None else ""
    if configured_root:
        return Path(configured_root).expanduser()

    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        workspace_owner = f"uid-{getuid()}"
    else:
        username = environ.get("USER") or environ.get("USERNAME")
        if username:
            workspace_owner = f"user-{_slugify_workspace_owner(username)}"
        else:
            workspace_owner = f"pid-{os.getpid()}"
    return Path(tempfile.gettempdir()) / f"code-agent-workspaces-{workspace_owner}"


def _build_clone_command(repo_url: str, destination: Path, branch: str | None) -> list[str]:
    """Build the git clone command for a workspace repo."""
    command = ["git", "clone"]
    if branch is not None:
        command.extend(["--branch", branch, "--single-branch"])
    command.extend(["--", repo_url, str(destination)])
    return command


def _should_delete_workspace(policy: WorkspaceCleanupPolicy, *, succeeded: bool) -> bool:
    """Return whether the cleanup policy should delete the workspace."""
    if succeeded:
        return policy.delete_on_success
    return not policy.retain_on_failure


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS,
) -> None:
    """Run a command and raise a workspace-specific error on failure."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceManagerError(f"Command timed out after {timeout}s") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        message = stderr or stdout or "command failed without output"
        if len(message) > 1024:
            message = message[:1024] + "... (truncated)"
        cmd_str = _mask_url_credentials(shlex.join(command))
        raise WorkspaceManagerError(f"Command failed ({cmd_str}): {message}")


class WorkspaceManager:
    """Provision and clean up per-task workspaces."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        cleanup_policy: WorkspaceCleanupPolicy | None = None,
        command_timeout: int = DEFAULT_SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.cleanup_policy = cleanup_policy or WorkspaceCleanupPolicy()
        self.command_timeout = command_timeout
        self._command_runner = command_runner or _run_command

    def create_workspace(self, request: WorkspaceRequest) -> WorkspaceHandle:
        """Create a unique task workspace and clone the repo into it."""
        self.root_dir.mkdir(parents=True, exist_ok=True)

        workspace_id = _build_workspace_id(request.task_id, request.attempt)
        workspace_path = self.root_dir / workspace_id
        # T-180: Merge workspace root and repository root for path consistency
        repo_path = workspace_path

        logger.info(
            "Creating sandbox workspace",
            extra={
                "workspace_id": workspace_id,
                "task_id": request.task_id,
                "repo_url": _mask_url_credentials(request.repo_url),
                "branch": request.branch,
            },
        )

        try:
            if workspace_path.exists():
                if (workspace_path / ".git").is_dir():
                    logger.info(
                        "Reusing existing workspace directory",
                        extra={"workspace_id": workspace_id, "task_id": request.task_id},
                    )
                    return WorkspaceHandle(
                        workspace_id=workspace_id,
                        task_id=request.task_id,
                        workspace_path=workspace_path,
                        repo_path=repo_path,
                        repo_url=request.repo_url,
                        branch=request.branch,
                        cleanup_policy=request.cleanup_policy or self.cleanup_policy,
                    )
                elif any(workspace_path.iterdir()):
                    raise WorkspaceManagerError(
                        f"Workspace directory exists and is not empty: {workspace_id}"
                    )
                else:
                    logger.info(
                        "Workspace directory %s exists and is empty. Proceeding with clone.",
                        workspace_id,
                    )
            else:
                workspace_path.mkdir(parents=False)
        except FileExistsError:
            # Race condition check
            if any(workspace_path.iterdir()):
                raise WorkspaceManagerError(f"Failed to create workspace directory: {workspace_id}")
        except Exception as exc:
            raise WorkspaceManagerError(f"Failed to prepare workspace directory: {exc}")

        try:
            if request.workspace_mode == WorkspaceMode.CLONE:
                if not request.repo_url:
                    raise WorkspaceManagerError("repo_url is required for CLONE mode")
                self._command_runner(
                    _build_clone_command(request.repo_url, repo_path, request.branch),
                    timeout=self.command_timeout,
                )
            elif request.workspace_mode == WorkspaceMode.INIT:
                self._command_runner(["git", "init"], cwd=repo_path, timeout=self.command_timeout)
            elif request.workspace_mode == WorkspaceMode.NONE:
                pass  # directory already created
            else:
                raise WorkspaceManagerError(f"Unknown workspace mode: {request.workspace_mode}")
        except Exception:
            shutil.rmtree(workspace_path, ignore_errors=True)
            logger.exception(
                "Failed to create sandbox workspace",
                extra={"workspace_id": workspace_id, "task_id": request.task_id},
            )
            raise

        return WorkspaceHandle(
            workspace_id=workspace_id,
            task_id=request.task_id,
            workspace_path=workspace_path,
            repo_path=repo_path,
            repo_url=request.repo_url,
            branch=request.branch,
            cleanup_policy=request.cleanup_policy or self.cleanup_policy,
        )

    def get_workspace(
        self,
        workspace_id: str,
        *,
        repo_url: str | None = None,
        branch: str | None = None,
        task_id: str | None = None,
    ) -> WorkspaceHandle:
        """Retrieve a handle for an existing workspace without re-cloning."""
        workspace_path = (self.root_dir / workspace_id).resolve()
        if not workspace_path.is_relative_to(self.root_dir) or workspace_path == self.root_dir:
            raise WorkspaceManagerError(f"Refusing to access path outside root: {workspace_path}")

        if not workspace_path.is_dir():
            raise WorkspaceManagerError(f"Workspace directory missing: {workspace_id}")

        repo_path = workspace_path

        # Note: We trust the caller for repo_url/branch/task_id if they provide them,
        # otherwise we just pass back what we can resolve.
        return WorkspaceHandle(
            workspace_id=workspace_id,
            task_id=task_id or "unknown",
            workspace_path=workspace_path,
            repo_path=repo_path,
            repo_url=repo_url or "unknown",
            branch=branch,
            cleanup_policy=self.cleanup_policy,
        )

    def cleanup_workspace(self, workspace: WorkspaceHandle, *, succeeded: bool) -> bool:
        """Delete or retain a workspace based on the cleanup policy."""
        should_delete = _should_delete_workspace(workspace.cleanup_policy, succeeded=succeeded)
        if not should_delete:
            logger.info(
                "Retaining sandbox workspace",
                extra={
                    "workspace_id": workspace.workspace_id,
                    "task_id": workspace.task_id,
                    "succeeded": succeeded,
                },
            )
            return False

        try:
            target = workspace.workspace_path.resolve()
            if not target.is_relative_to(self.root_dir) or target == self.root_dir:
                raise WorkspaceManagerError(f"Refusing to delete path outside root: {target}")
            shutil.rmtree(target)
        except FileNotFoundError:
            return True
        except OSError as exc:
            raise WorkspaceManagerError(
                f"Failed to remove workspace {workspace.workspace_id}: {exc}"
            ) from exc

        logger.info(
            "Deleted sandbox workspace",
            extra={
                "workspace_id": workspace.workspace_id,
                "task_id": workspace.task_id,
                "succeeded": succeeded,
            },
        )
        return True
