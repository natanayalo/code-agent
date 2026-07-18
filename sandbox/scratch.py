"""Deterministic, filesystem-safe writable namespaces for node execution."""

from __future__ import annotations

import hashlib
from pathlib import Path


def scratch_namespace_component(namespace: str | None) -> str:
    """Return a stable path component without trusting planner-provided text."""
    if not namespace:
        return "default"
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
    return f"node-{digest[:32]}"


def node_run_root(workspace_path: Path, namespace: str | None) -> Path:
    """Return the isolated writable root for one node execution."""
    return node_scratch_root(workspace_path, namespace) / "code-agent"


def node_scratch_root(workspace_path: Path, namespace: str | None) -> Path:
    """Return a node-private writable root that is never inside the repository."""
    return (
        workspace_path.parent
        / ".code-agent-scratch"
        / workspace_path.name
        / scratch_namespace_component(namespace)
    )


def node_agent_home(workspace_path: Path, namespace: str | None) -> Path:
    """Return the isolated provider home for one node execution."""
    return node_scratch_root(workspace_path, namespace) / "agent-home"


def node_artifacts_root(workspace_path: Path, namespace: str | None) -> Path:
    """Return the isolated artifact root for one node execution."""
    return node_scratch_root(workspace_path, namespace) / "artifacts"
