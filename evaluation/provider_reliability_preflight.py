"""Pre-dispatch evidence predicates for provider reliability extraction."""

from __future__ import annotations

from db.enums import ArtifactType
from db.models import Task


def _is_preflight_rejection_artifact(entry: dict[str, object]) -> bool:
    """Return whether an artifact records a rejection before provider execution."""
    if entry.get("artifact_type") != ArtifactType.PRE_DISPATCH_DIAGNOSTICS.value:
        return False
    metadata = entry.get("artifact_metadata")
    if not isinstance(metadata, dict):
        return False
    nested_metadata = metadata.get(ArtifactType.PRE_DISPATCH_DIAGNOSTICS.value)
    if isinstance(nested_metadata, dict):
        metadata = nested_metadata
    return (
        metadata.get("decision") == "preflight_rejected"
        and metadata.get("execution_started") is False
    )


def task_was_only_preflight_rejected(task: Task) -> bool:
    """Return whether every worker run stopped at the pre-dispatch gate."""
    runs = list(task.worker_runs or [])
    if not runs:
        return False
    for run in runs:
        diagnostics = [
            entry
            for entry in (run.artifact_index or [])
            if isinstance(entry, dict)
            and entry.get("artifact_type") == ArtifactType.PRE_DISPATCH_DIAGNOSTICS.value
        ]
        if not diagnostics or not all(
            _is_preflight_rejection_artifact(entry) for entry in diagnostics
        ):
            return False
    return True
