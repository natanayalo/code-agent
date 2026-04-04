"""Action and file path policy for sandbox execution."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from sandbox.workspace import SandboxModel


class PathPolicy(SandboxModel):
    """Explicit allow/deny policy for file paths in the sandbox."""

    allowed_prefixes: list[str] = Field(default_factory=lambda: ["/workspace"])
    denied_prefixes: list[str] = Field(default_factory=lambda: ["/workspace/.git"])

    def check_path(self, path: str | Path) -> bool:
        """Return True if the path is allowed by the policy."""
        try:
            # We use string-based prefix matching for sandbox paths.
            # These are usually absolute paths inside the container (e.g. /workspace/repo).
            path_str = str(path)

            # Deny takes precedence
            for denied in sorted(self.denied_prefixes, key=len, reverse=True):
                if path_str.startswith(denied):
                    return False

            # Must be in an allowed prefix
            for allowed in sorted(self.allowed_prefixes, key=len, reverse=True):
                if path_str.startswith(allowed):
                    return True

            # Default to deny if no allowed prefix matches
            return False
        except Exception:
            return False
