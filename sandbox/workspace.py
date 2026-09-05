"""Workspace management helpers for sandboxed task execution."""

from __future__ import annotations

import base64
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from sandbox.constants import DEFAULT_SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS
from sandbox.redact import mask_url_credentials as _mask_url_credentials
from sandbox.scratch import workspace_scratch_root

logger = logging.getLogger(__name__)


class CommandRunner(Protocol):
    """Protocol for running external commands."""

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = DEFAULT_SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS,
        env: Mapping[str, str] | None = None,
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


class WorkspaceMode(StrEnum):
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
    git_token: str | None = Field(default=None, repr=False)


class WorkspaceHandle(SandboxModel):
    """Details for a provisioned task workspace."""

    workspace_id: str
    task_id: str
    workspace_path: Path
    repo_path: Path
    repo_url: str
    branch: str | None = None
    trusted_git_dir: Path | None = None
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


def _trusted_git_dir(workspace_path: Path, workspace_id: str) -> Path:
    """Return the broker-authoritative GIT_DIR path for a workspace."""
    return workspace_path.parent / ".code-agent-git" / workspace_id


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


def _is_github_repo_url(repo_url: str | None) -> bool:
    """Check whether a repository URL targets github.com."""
    if not repo_url:
        return False
    stripped = repo_url.strip()
    if not stripped:
        return False
    try:
        parsed = urlparse(stripped)
        netloc = parsed.netloc.lower()
        if "@" in netloc:
            netloc = netloc.split("@")[-1]
        if netloc in {"github.com", "www.github.com"}:
            return True
    except Exception:
        pass
    return stripped.startswith(("git@github.com:", "github.com/"))


def build_authenticated_github_git_env(
    token: str | None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build broker Git environment with GitHub authorization header.

    Preserves any existing git configs in the base environment.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if not token or not token.strip():
        return env

    encoded_token = base64.b64encode(f"x-access-token:{token.strip()}".encode()).decode()
    header_key = "http.https://github.com/.extraheader"
    header_val = f"Authorization: Basic {encoded_token}"

    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0

    target_idx = None
    for i in range(count):
        if env.get(f"GIT_CONFIG_KEY_{i}") == header_key:
            target_idx = i
            break

    if target_idx is not None:
        env[f"GIT_CONFIG_VALUE_{target_idx}"] = header_val
    else:
        env[f"GIT_CONFIG_KEY_{count}"] = header_key
        env[f"GIT_CONFIG_VALUE_{count}"] = header_val
        env["GIT_CONFIG_COUNT"] = str(count + 1)

    return env


def _redact_git_error_message(message: str, env: Mapping[str, str]) -> str:
    redacted = _mask_url_credentials(message)
    for key, value in env.items():
        if key.startswith("GIT_CONFIG_VALUE_") and "Authorization: Basic " in value:
            b64_part = value.split("Authorization: Basic ", 1)[-1].strip()
            if b64_part:
                redacted = redacted.replace(b64_part, "[REDACTED]")
        elif key in {"GH_TOKEN", "GITHUB_TOKEN"} and value.strip():
            redacted = redacted.replace(value.strip(), "[REDACTED]")
    return redacted


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = DEFAULT_SANDBOX_MAX_COMMAND_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> None:
    """Run a command and raise a workspace-specific error on failure."""
    effective_env = os.environ.copy() if env is None else dict(env)
    effective_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=effective_env,
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
        redacted_message = _redact_git_error_message(message, effective_env)
        cmd_str = _mask_url_credentials(shlex.join(command))
        raise WorkspaceManagerError(f"Command failed ({cmd_str}): {redacted_message}")


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

    def _workspace_handle(
        self,
        request: WorkspaceRequest,
        *,
        workspace_id: str,
        workspace_path: Path,
        repo_path: Path,
    ) -> WorkspaceHandle:
        return WorkspaceHandle(
            workspace_id=workspace_id,
            task_id=request.task_id,
            workspace_path=workspace_path,
            repo_path=repo_path,
            repo_url=request.repo_url,
            branch=request.branch,
            trusted_git_dir=_trusted_git_dir(workspace_path, workspace_id),
            workspace_mode=request.workspace_mode,
            cleanup_policy=request.cleanup_policy or self.cleanup_policy,
        )

    def _prepare_workspace_directory(
        self,
        *,
        request: WorkspaceRequest,
        workspace_id: str,
        workspace_path: Path,
        repo_path: Path,
    ) -> WorkspaceHandle | None:
        try:
            if workspace_path.exists():
                if (workspace_path / ".git").is_dir() or _trusted_git_dir(
                    workspace_path, workspace_id
                ).is_dir():
                    trusted_git_dir = _trusted_git_dir(workspace_path, workspace_id)
                    if not trusted_git_dir.exists():
                        raise WorkspaceManagerError(
                            f"Workspace {workspace_id} exists but trusted GIT_DIR is missing. "
                            "Refusing to reprovision from potentially attacker-controlled workspace/.git."  # noqa: E501
                        )
                    logger.info(
                        "Reusing existing workspace directory and trusted GIT_DIR",
                        extra={"workspace_id": workspace_id, "task_id": request.task_id},
                    )
                    return self._workspace_handle(
                        request,
                        workspace_id=workspace_id,
                        workspace_path=workspace_path,
                        repo_path=repo_path,
                    )
                if any(workspace_path.iterdir()):
                    raise WorkspaceManagerError(
                        f"Workspace directory exists and is not empty: {workspace_id}"
                    )
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
            raise WorkspaceManagerError(f"Failed to prepare workspace directory: {exc}") from exc
        return None

    def _establish_trusted_git_authority(
        self, workspace_path: Path, trusted_git_dir: Path, clone_url: str
    ) -> None:
        """Move the cloned .git to a broker-owned location and verify parity."""
        workspace_git = workspace_path / ".git"
        if not workspace_git.exists():
            return  # INIT mode without initial git, or NONE mode

        if trusted_git_dir.exists():
            shutil.rmtree(trusted_git_dir, ignore_errors=True)

        shutil.copytree(workspace_git, trusted_git_dir, symlinks=True)

        # Verify parity
        try:
            # broker HEAD == workspace HEAD
            head_cmd = ["git", "--git-dir", str(trusted_git_dir), "rev-parse", "HEAD"]
            proc = subprocess.run(head_cmd, capture_output=True, text=True, check=True)
            broker_head = proc.stdout.strip()

            proc = subprocess.run(
                ["git", "--git-dir", str(workspace_git), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            workspace_head = proc.stdout.strip()

            if broker_head != workspace_head:
                raise WorkspaceManagerError("Trusted GIT_DIR HEAD does not match workspace HEAD")

            # verify remote origin URL
            remote_cmd = [
                "git",
                "--git-dir",
                str(trusted_git_dir),
                "config",
                "--get",
                "remote.origin.url",
            ]
            proc = subprocess.run(remote_cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                broker_url = proc.stdout.strip()
                if broker_url != clone_url:
                    raise WorkspaceManagerError(
                        f"Trusted GIT_DIR remote origin mismatch: expected {clone_url}, got {broker_url}"  # noqa: E501
                    )

            # verify no uncommitted changes
            diff_cmd = [
                "git",
                "--git-dir",
                str(trusted_git_dir),
                "--work-tree",
                str(workspace_path),
                "-c",
                "core.hooksPath=/dev/null",
                "diff",
                "--quiet",
                "HEAD",
            ]
            proc = subprocess.run(diff_cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise WorkspaceManagerError(
                    "Trusted GIT_DIR indicates uncommitted changes immediately after clone"
                )

        except Exception as e:
            shutil.rmtree(trusted_git_dir, ignore_errors=True)
            raise WorkspaceManagerError(f"Failed to verify trusted GIT_DIR parity: {e}") from e

    def _initialize_workspace(self, request: WorkspaceRequest, repo_path: Path) -> None:
        if request.workspace_mode == WorkspaceMode.CLONE:
            if not request.repo_url:
                raise WorkspaceManagerError("repo_url is required for CLONE mode")
            clone_cmd = _build_clone_command(request.repo_url, repo_path, request.branch)
            clone_env = (
                build_authenticated_github_git_env(request.git_token)
                if (request.git_token and _is_github_repo_url(request.repo_url))
                else None
            )
            if clone_env is not None:
                try:
                    self._command_runner(
                        clone_cmd,
                        timeout=self.command_timeout,
                        env=clone_env,
                    )
                except TypeError:
                    self._command_runner(clone_cmd, timeout=self.command_timeout)
            else:
                self._command_runner(clone_cmd, timeout=self.command_timeout)
        elif request.workspace_mode == WorkspaceMode.INIT:
            self._command_runner(["git", "init"], cwd=repo_path, timeout=self.command_timeout)
        elif request.workspace_mode == WorkspaceMode.NONE:
            pass  # directory already created
        else:
            raise WorkspaceManagerError(f"Unknown workspace mode: {request.workspace_mode}")

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

        existing_handle = self._prepare_workspace_directory(
            request=request,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            repo_path=repo_path,
        )
        if existing_handle is not None:
            return existing_handle

        try:
            self._initialize_workspace(request, repo_path)
            if request.workspace_mode == WorkspaceMode.CLONE:
                trusted_git_dir = _trusted_git_dir(workspace_path, workspace_id)
                self._establish_trusted_git_authority(
                    workspace_path, trusted_git_dir, request.repo_url
                )
        except Exception:
            shutil.rmtree(workspace_path, ignore_errors=True)
            logger.exception(
                "Failed to create sandbox workspace",
                extra={"workspace_id": workspace_id, "task_id": request.task_id},
            )
            raise

        return self._workspace_handle(
            request,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            repo_path=repo_path,
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
            trusted_git_dir=_trusted_git_dir(workspace_path, workspace_id),
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
            scratch_root = workspace_scratch_root(target)
            scratch_parent = (self.root_dir / ".code-agent-scratch").resolve()
            if not scratch_root.is_relative_to(scratch_parent):
                raise WorkspaceManagerError("Refusing to delete scratch outside root")
            shutil.rmtree(scratch_root, ignore_errors=True)

            if workspace.trusted_git_dir:
                trusted_git = workspace.trusted_git_dir.resolve()
                trusted_parent = (self.root_dir / ".code-agent-git").resolve()
                if trusted_git.is_relative_to(trusted_parent):
                    shutil.rmtree(trusted_git, ignore_errors=True)
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
