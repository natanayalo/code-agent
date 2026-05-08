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
