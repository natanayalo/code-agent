"""Serialization helpers for execution-path orchestration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from db.enums import ArtifactType
from workers import ArtifactReference

logger = logging.getLogger(__name__)


def _enum_value(value: object | None) -> str | None:
    """Normalize enum-backed ORM values into plain strings."""
    if value is None:
        return None
    member_value = getattr(value, "value", None)
    if isinstance(member_value, str):
        return member_value
    return str(value)


def _workspace_id_from_artifacts(artifacts: list[ArtifactReference]) -> str | None:
    """Infer the workspace id from the retained workspace artifact path."""
    for artifact in artifacts:
        if artifact.artifact_type == ArtifactType.WORKSPACE.value or artifact.name == "workspace":
            parsed_uri = urlparse(artifact.uri)
            candidate = ""
            if parsed_uri.scheme and parsed_uri.path:
                candidate = Path(unquote(parsed_uri.path)).name.strip()
            elif parsed_uri.scheme and parsed_uri.netloc:
                candidate = parsed_uri.netloc.strip()
            else:
                candidate = Path(unquote(artifact.uri)).name.strip()
            if candidate:
                return candidate
    return None


def _artifact_type_for_persistence(artifact: ArtifactReference) -> str | None:
    """Return a DB-supported artifact type for the emitted artifact."""
    if artifact.artifact_type is None:
        return None
    try:
        return ArtifactType(artifact.artifact_type).value
    except ValueError:
        logger.warning(
            "Skipping unsupported artifact type during execution-path persistence",
            extra={"artifact_name": artifact.name, "artifact_type": artifact.artifact_type},
        )
        return None


def _serialize_verification_report(report: object | None) -> dict[str, Any] | None:
    """Normalize verification state from either a Pydantic model or a raw mapping."""
    if report is None:
        return None

    def _drop_none_reason_codes(value: Any) -> Any:
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key, item in value.items():
                if key == "reason_code" and item is None:
                    continue
                output[str(key)] = _drop_none_reason_codes(item)
            return output
        if isinstance(value, list):
            return [_drop_none_reason_codes(item) for item in value]
        return value

    if hasattr(report, "model_dump"):
        serialized = report.model_dump(mode="json")
        serialized = _drop_none_reason_codes(serialized)
        if serialized.get("failure_kind") is None:
            serialized.pop("failure_kind", None)
        if serialized.get("deterministic_verification") is None:
            serialized.pop("deterministic_verification", None)
        return serialized
    if isinstance(report, Mapping):
        serialized = _drop_none_reason_codes(dict(report))
        if serialized.get("deterministic_verification") is None:
            serialized.pop("deterministic_verification", None)
        return serialized
    raise TypeError(f"Unsupported verification report type: {type(report).__name__}")


def _to_json_compatible(value: object) -> Any:
    """Recursively convert nested model/mapping payloads into JSON-compatible values."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _to_json_compatible(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_json_compatible(item) for item in value]
    return value


def _serialize_review_result(review_result: object | None) -> dict[str, Any] | None:
    """Normalize review output from either a Pydantic model or a raw mapping."""
    if review_result is None:
        return None
    if hasattr(review_result, "model_dump"):
        return review_result.model_dump(mode="json")
    if isinstance(review_result, Mapping):
        return _to_json_compatible(review_result)
    raise TypeError(f"Unsupported review result type: {type(review_result).__name__}")


def _review_result_artifact_entry(
    review_result: object | None,
    *,
    artifact_type: str = ArtifactType.REVIEW_RESULT.value,
) -> dict[str, Any] | None:
    """Build a structured artifact index entry for a review payload when present."""
    serialized = _serialize_review_result(review_result)
    if serialized is None:
        return None
    return {
        "name": artifact_type,
        "uri": f"inline://{artifact_type}",
        "artifact_type": artifact_type,
        "artifact_metadata": {artifact_type: serialized},
    }


def _approval_constraints_payload(
    *,
    status: str,
    approval_type: str | None,
    reason: str | None,
    resume_token: str | None,
    updated_at: datetime,
    source: str,
    approved: bool | None = None,
) -> dict[str, Any]:
    """Build the persisted approval checkpoint payload stored in task constraints."""
    payload: dict[str, Any] = {
        "status": status,
        "approval_type": approval_type,
        "reason": reason,
        "resume_token": resume_token,
        "updated_at": updated_at.isoformat(),
        "source": source,
    }
    if approved is not None:
        payload["approved"] = approved
    return payload


def _get_trace_id_from_context(context: dict[str, str] | None) -> str | None:
    """Extract the 32-char hex trace ID from a W3C traceparent context."""
    if not context:
        return None
    traceparent = context.get("traceparent")
    if not traceparent:
        return None
    parts = traceparent.split("-")
    if len(parts) >= 2:
        return parts[1]
    return None
