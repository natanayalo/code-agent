"""Fail-closed verification of the legacy worker's empty-diff placeholder."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def has_clean_trusted_workspace(worker: Any, workspace_id: str | None) -> bool:
    """Confirm a retained workspace has no staged, unstaged, or untracked changes."""
    manager = getattr(worker, "workspace_manager", None)
    root = getattr(manager, "root_dir", None)
    if not root or not workspace_id:
        logger.warning("Cannot verify read-only workspace: missing workspace identity")
        return False
    root_path = Path(root).resolve()
    workspace = (root_path / workspace_id).resolve()
    if workspace == root_path or not workspace.is_relative_to(root_path):
        logger.warning("Cannot verify read-only workspace: invalid workspace identity")
        return False
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("Cannot verify read-only workspace Git status", exc_info=True)
        return False
    if result.returncode != 0:
        logger.warning(
            "Cannot verify read-only workspace Git status", extra={"exit_code": result.returncode}
        )
    return result.returncode == 0 and not result.stdout
