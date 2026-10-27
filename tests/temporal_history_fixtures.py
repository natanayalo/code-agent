"""Capture and replay sanitized, frozen Temporal history fixtures."""

from __future__ import annotations

import os
import re
from pathlib import Path

from temporalio.client import WorkflowHistory


def capture_history_fixture(name: str, history: WorkflowHistory) -> None:
    """Write a sanitized history only when explicitly generating fixtures."""
    fixture_dir = os.environ.get("TEMPORAL_HISTORY_FIXTURE_DIR")
    if not fixture_dir:
        return
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError(f"Invalid history fixture name: {name}")
    sanitized = re.sub(r"compat-[a-z-]+-[a-f0-9]{32}", "fixture-task", history.to_json())
    path = Path(fixture_dir) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sanitized + "\n", encoding="utf-8")
