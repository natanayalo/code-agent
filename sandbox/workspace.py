"""Workspace management helpers for sandboxed task execution."""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class CommandRunner(Protocol):
    """Protocol for running external commands."""

    def __call__(self, command: list[str], *, cwd: Path | None = None) -> None: ...


class SandboxModel(BaseModel):
    """Base model for sandbox boundary objects."""

    model_config = ConfigDict(extra="forbid")


class WorkspaceCleanupPolicy(SandboxModel):
    """Cleanup rules for a task workspace."""

    delete_on_success: bool = True
    retain_on_failure: bool = True


class WorkspaceRequest(SandboxModel):
    """Input required to provision a task workspace."""

    task_id: str = Field(min_length=1)
    repo_url: str = Field(min_length=1)
    branch: str | None = None


class WorkspaceHandle(SandboxModel):
    """Details for a provisioned task workspace."""

    workspace_id: str
    task_id: str
    workspace_path: Path
    repo_path: Path
    repo_url: str
    branch: str | None = None
    cleanup_policy: WorkspaceCleanupPolicy


class WorkspaceManagerError(RuntimeError):
    """Raised when workspace provisioning or cleanup fails."""


def _slugify_task_id(task_id: str) -> str:
    """Normalize a task id for filesystem-safe workspace naming."""
    slug = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:64]
    return slug or "task"


def _build_workspace_id(task_id: str) -> str:
    """Generate a readable unique workspace identifier."""
    return f"workspace-{_slugify_task_id(task_id)}-{uuid4().hex[:8]}"


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


def _run_command(command: list[str], *, cwd: Path | None = None) -> None:
    """Run a command and raise a workspace-specific error on failure."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    timeout = 300
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
        cmd_str = re.sub(r"://[^/ ]+@", "://****@", shlex.join(command))
        raise WorkspaceManagerError(f"Command failed ({cmd_str}): {message}")


class WorkspaceManager:
    """Provision and clean up per-task workspaces."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        cleanup_policy: WorkspaceCleanupPolicy | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.cleanup_policy = cleanup_policy or WorkspaceCleanupPolicy()
        self._command_runner = command_runner or _run_command

    def create_workspace(self, request: WorkspaceRequest) -> WorkspaceHandle:
        """Create a unique task workspace and clone the repo into it."""
        self.root_dir.mkdir(parents=True, exist_ok=True)

        workspace_id = _build_workspace_id(request.task_id)
        workspace_path = self.root_dir / workspace_id
        repo_path = workspace_path / "repo"

        logger.info(
            "Creating sandbox workspace",
            extra={
                "workspace_id": workspace_id,
                "task_id": request.task_id,
                "repo_url": re.sub(r"://[^/ ]+@", "://****@", request.repo_url),
                "branch": request.branch,
            },
        )

        try:
            workspace_path.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            raise WorkspaceManagerError(f"Workspace directory already exists: {workspace_id}")

        try:
            self._command_runner(
                _build_clone_command(request.repo_url, repo_path, request.branch),
            )
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
