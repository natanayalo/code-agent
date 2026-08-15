"""Private task-capture helpers for M28 real-worker evidence."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from evaluation.m28_real_worker_models import PairMeasurements

_COMPACT_SESSION_KEYS = (
    "active_goal",
    "decisions_made",
    "identified_risks",
    "files_touched",
)
_EVIDENCE_ARTIFACT_NAMES = frozenset(
    {"native-agent-stdout", "native-agent-events", "native-agent-provider-log"}
)
_COMMAND_MARKERS = (
    "m28-useful_hit-marker",
    "m28-irrelevant_rejection-marker",
    "m28-stale_reverification-marker",
    "m28-conflict_handling-marker",
)


def _memory_event(task: dict[str, Any]) -> dict[str, Any]:
    for event in reversed(task.get("timeline") or []):
        if event.get("event_type") == "memory_loaded" and isinstance(event.get("payload"), dict):
            return event["payload"]
    return {}


def _compact_session_context(value: object) -> dict[str, Any]:
    """Keep only compact session fields in private evidence."""
    if not isinstance(value, dict):
        return {}
    return {key: value.get(key) for key in _COMPACT_SESSION_KEYS}


def _artifact_uris(task: dict[str, Any]) -> list[str]:
    """Return private run artifact locations without copying them to public reports."""
    artifacts = (task.get("latest_run") or {}).get("artifacts") or []
    return sorted(
        str(artifact["uri"])
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("uri")
    )


def _command_markers_from_artifacts(task: dict[str, Any]) -> list[str]:
    """Read marker evidence from retained native-agent artifacts."""
    artifacts = (task.get("latest_run") or {}).get("artifacts") or []
    artifact_text = ""
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("name") not in _EVIDENCE_ARTIFACT_NAMES:
            continue
        parsed = urlparse(str(artifact.get("uri") or ""))
        if parsed.scheme != "file":
            continue
        try:
            artifact_text += Path(unquote(parsed.path)).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
    return [marker for marker in _COMMAND_MARKERS if marker in artifact_text]


def _measure(task: dict[str, Any], *, session_continuity: bool = False) -> PairMeasurements:
    event = _memory_event(task)
    created, updated = task.get("created_at"), task.get("updated_at")
    elapsed = (
        max(
            0.0, (datetime.fromisoformat(updated) - datetime.fromisoformat(created)).total_seconds()
        )
        if created and updated
        else None
    )
    interactions = task.get("interactions") or []
    return PairMeasurements(
        terminal_status=str(task.get("status")),
        memory_keys=sorted(event.get("accepted_keys") or []),
        suppressed_keys=sorted(event.get("suppressed_keys") or []),
        accepted_reason_codes=sorted(
            {
                reason
                for item in event.get("accepted_details") or []
                for reason in item.get("reason_codes") or []
            }
        ),
        command_markers=_command_markers_from_artifacts(task),
        questions=sum(
            1 for item in interactions if item.get("interaction_type") == "clarification"
        ),
        interventions=sum(
            1 for item in interactions if item.get("status") in {"resolved", "rejected"}
        ),
        time_to_terminal_seconds=elapsed,
        session_continuity=session_continuity,
    )
