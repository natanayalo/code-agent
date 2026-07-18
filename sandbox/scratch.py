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
    return workspace_path / ".code-agent" / "node-runs" / scratch_namespace_component(namespace)
