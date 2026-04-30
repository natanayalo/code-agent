"""Shared configuration for API system-wide settings."""

from __future__ import annotations

import os
from dataclasses import dataclass

from sandbox.container import DEFAULT_SANDBOX_IMAGE
from sandbox.workspace import default_workspace_root


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """Consolidated system-level configuration."""

    default_image: str
    workspace_root: str

    @classmethod
    def load_from_env(cls) -> SystemConfig:
        """Load and normalize system configuration from environment variables."""
        image = os.environ.get("CODE_AGENT_SANDBOX_IMAGE", "").strip() or DEFAULT_SANDBOX_IMAGE
        workspace_root = os.environ.get("CODE_AGENT_WORKSPACE_ROOT", "").strip() or str(
            default_workspace_root()
        )

        return cls(
            default_image=image,
            workspace_root=workspace_root,
        )
