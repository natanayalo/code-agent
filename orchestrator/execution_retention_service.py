"""Retention cleanup helpers for persisted execution artifacts."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from repositories import ArtifactRepository, WorkerRunRepository, session_scope
from sandbox.scratch import workspace_scratch_root

logger = logging.getLogger("orchestrator.execution")


def _workspace_path_for_run(self: Any, workspace_id: str | None) -> Path | None:
    """Resolve the on-disk workspace path for a retained run, if configured."""
    if workspace_id is None or self.workspace_root is None:
        return None

    workspace_path = (self.workspace_root / workspace_id).resolve()
    try:
        if not workspace_path.is_relative_to(self.workspace_root):
            logger.warning(
                "Skipping retention cleanup outside the configured workspace root.",
                extra={
                    "workspace_root": str(self.workspace_root),
                    "workspace_path": str(workspace_path),
                },
            )
            return None
    except ValueError:
        return None
    return workspace_path


def _delete_retained_workspace_path(self: Any, workspace_id: str | None) -> bool:
    """Delete a retained workspace directory from disk, if configured."""
    workspace_path = self._workspace_path_for_run(workspace_id)
    if workspace_path is None:
        return False

    try:
        deleted = False
        if workspace_path.exists():
            shutil.rmtree(workspace_path)
            deleted = True
        scratch_root = workspace_scratch_root(workspace_path)
        scratch_parent = (self.workspace_root / ".code-agent-scratch").resolve()
        if not scratch_root.is_relative_to(scratch_parent):
            return deleted
        if scratch_root.exists():
            shutil.rmtree(scratch_root)
            deleted = True
        return deleted
    except OSError as exc:
        logger.warning(
            "Failed to delete retained workspace directory",
            exc_info=exc,
            extra={"workspace_path": str(workspace_path)},
        )
        return False


def _prune_retained_runs(self: Any, *, now: datetime) -> int:
    """Delete retained artifact rows and workspace directories for expired runs."""
    if self.retention_seconds is None:
        return 0

    deleted_runs = 0
    with session_scope(self.session_factory) as session:
        worker_run_repo = WorkerRunRepository(session)
        artifact_repo = ArtifactRepository(session)

        for worker_run in worker_run_repo.list_retained_before(now):
            artifact_repo.delete_by_run(worker_run.id)
            worker_run_repo.clear_artifact_index(worker_run.id)
            worker_run.retention_expires_at = None
            if self._delete_retained_workspace_path(worker_run.workspace_id):
                logger.info(
                    "Deleted retained sandbox workspace",
                    extra={
                        "worker_run_id": worker_run.id,
                        "workspace_id": worker_run.workspace_id,
                    },
                )
                deleted_runs += 1

    if deleted_runs:
        logger.info(
            "Pruned retained execution artifacts",
            extra={
                "deleted_runs": deleted_runs,
                "workspace_root": str(self.workspace_root) if self.workspace_root else None,
                "retention_seconds": self.retention_seconds,
            },
        )
    return deleted_runs
