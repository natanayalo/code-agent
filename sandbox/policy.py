"""Action and file path policy for sandbox execution."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from sandbox.workspace import SandboxModel


class PathPolicy(SandboxModel):
    """Explicit allow/deny policy for file paths in the sandbox."""

    allowed_prefixes: list[str] = Field(default_factory=lambda: ["/workspace"])
    denied_prefixes: list[str] = Field(default_factory=lambda: ["/workspace/.git"])

    def check_path(self, path: str | Path) -> bool:
        """Return True if path is allowed, False if it is explicitly denied or prefix missing."""
        p = Path(os.path.normpath(str(path)))
        # Deny takes precedence
        for denied in self.denied_prefixes:
            try:
                if p.is_relative_to(denied):
                    return False
            except ValueError:
                continue

        # Must be in an allowed prefix
        for allowed in self.allowed_prefixes:
            try:
                if p.is_relative_to(allowed):
                    return True
            except ValueError:
                continue

        return False


def is_in_container() -> bool:
    """Return True if the current process appears to be running inside a container."""
    # Common indicator for Docker
    if os.path.exists("/.dockerenv"):
        return True

    # Check cgroup for 'docker' or 'containerd'
    cgroup_path = Path("/proc/1/cgroup")
    if cgroup_path.exists():
        try:
            content = cgroup_path.read_text()
            return "docker" in content or "containerd" in content or "kubepods" in content
        except OSError:
            pass

    return False


class LocalRepoPolicyError(RuntimeError):
    """Raised when a local repository path violates sandbox mounting policies."""


def is_allowed_local_remote(resolved_path: Path) -> bool:
    """Return True if the resolved path is inside an explicitly allowed local remote directory."""
    allowed_remotes_env = os.environ.get("CODE_AGENT_ALLOWED_LOCAL_REMOTES", "")
    for path_text in allowed_remotes_env.split(","):
        path_text = path_text.strip()
        if path_text and resolved_path.is_relative_to(Path(path_text).resolve()):
            return True
    return False


def raise_if_sibling_workspace(
    *,
    resolved_path: Path,
    workspace_path: Path | None,
    allowed_root: Path,
) -> None:
    """Raise LocalRepoPolicyError if the resolved path points to a sibling workspace."""
    if workspace_path is not None:
        is_sibling = not resolved_path.is_relative_to(workspace_path)
        if resolved_path == workspace_path or not is_sibling:
            return

    rel_parts = resolved_path.relative_to(allowed_root).parts
    if rel_parts and rel_parts[0].startswith("workspace-"):
        raise LocalRepoPolicyError(
            f"Mounting or accessing sibling workspaces is forbidden: {resolved_path}"
        )


def validate_local_repo_path(local_repo_path: str, workspace_path: Path | None = None) -> None:
    """Validate a local repo path against the allowed workspace root and remote allowlist."""
    from sandbox.workspace import default_workspace_root

    resolved_path = Path(local_repo_path).resolve()
    allowed_root = default_workspace_root().resolve()

    if resolved_path == allowed_root:
        raise LocalRepoPolicyError(f"Accessing the workspace root {allowed_root} is forbidden")

    if resolved_path.is_relative_to(allowed_root):
        raise_if_sibling_workspace(
            resolved_path=resolved_path,
            workspace_path=workspace_path,
            allowed_root=allowed_root,
        )
        return

    if not is_allowed_local_remote(resolved_path):
        raise LocalRepoPolicyError(
            f"Local repo path {resolved_path} is outside the allowed workspace root {allowed_root}"
        )
