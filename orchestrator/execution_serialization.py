"""Serialization helpers for execution-path orchestration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel

from db.enums import ArtifactType, TaskStatus
from orchestrator.execution_types import ProgressPhase, TaskSnapshot
from orchestrator.state import OrchestratorState
from tools import coerce_permission_level
from workers import ArtifactReference, WorkerResult

logger = logging.getLogger(__name__)


def _enum_value(value: object | None) -> str | None:
    """Normalize enum-backed ORM values into plain strings."""
    if value is None:
        return None
    member_value = getattr(value, "value", None)
    if isinstance(member_value, str):
        return member_value
    return str(value)


def _interrupt_payload_from_object(interrupt: object) -> dict[str, Any] | None:
    """Extract an interrupt payload mapping from LangGraph interrupt objects."""
    if isinstance(interrupt, Mapping):
        candidate = interrupt.get("value")
        if isinstance(candidate, Mapping):
            return dict(candidate)
        return dict(interrupt)

    candidate = getattr(interrupt, "value", None)
    if isinstance(candidate, Mapping):
        return dict(candidate)
    return None


def _interrupt_summary(payloads: list[dict[str, Any]]) -> str:
    """Build a concise failure summary when orchestration stops on an interrupt."""
    first = payloads[0] if payloads else {}
    approval_type = str(first.get("approval_type") or "").strip()
    reason = str(first.get("reason") or "").strip()
    requested_permission_level = coerce_permission_level(first.get("requested_permission"))
    requested_permission = (
        requested_permission_level.value if requested_permission_level is not None else None
    )

    if approval_type == "permission_escalation":
        if requested_permission:
            summary = (
                f"Run paused pending permission escalation approval for '{requested_permission}'."
            )
        else:
            summary = "Run paused pending permission escalation approval."
    elif approval_type:
        display_approval_type = approval_type.replace("_", " ")
        suffix = "" if display_approval_type.endswith("approval") else " approval"
        summary = f"Run paused pending {display_approval_type}{suffix}."
    else:
        summary = "Run paused pending manual approval."

    if reason:
        summary = f"{summary} {reason}"
    return summary


def _extract_graph_payload(data: Any) -> Mapping[str, Any]:
    """Safely extract a mapping payload from a graph state object or model."""
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if isinstance(data, Mapping):
        return data
    return {}


def _summarize_graph_span_input(graph_input: Mapping[str, Any]) -> dict[str, Any]:
    """Build a compact graph span input payload to avoid emitting full task state."""
    task = _extract_graph_payload(graph_input.get("task"))
    session = _extract_graph_payload(graph_input.get("session"))
    task_spec = _extract_graph_payload(graph_input.get("task_spec"))
    budget = _extract_graph_payload(task.get("budget"))

    summary: dict[str, Any] = {
        "task_id": task.get("task_id"),
        "attempt_count": graph_input.get("attempt_count"),
        "channel": session.get("channel"),
        "branch": task.get("branch"),
        "task_type": task_spec.get("task_type"),
        "execution_mode": task.get("constraints", {}).get("execution_mode")
        if isinstance(task.get("constraints"), Mapping)
        else None,
        "max_iterations": budget.get("max_iterations"),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _summarize_graph_span_output(raw_output: object) -> dict[str, Any]:
    """Build a compact graph span output payload to avoid large span attributes."""
    payload = _extract_graph_payload(raw_output)
    if not payload and not isinstance(raw_output, Mapping | BaseModel):
        return {"output_type": type(raw_output).__name__}

    result = _extract_graph_payload(payload.get("result"))
    review = _extract_graph_payload(payload.get("review"))
    verification = _extract_graph_payload(payload.get("verification"))
    task = _extract_graph_payload(payload.get("task"))
    constraints = task.get("constraints")
    if not isinstance(constraints, Mapping):
        constraints = {}
    interactions = constraints.get("interactions")
    clarification_round = 0
    clarification_resolved = False
    if isinstance(interactions, Mapping):
        for interaction in interactions.values():
            if not isinstance(interaction, Mapping):
                continue
            if interaction.get("interaction_type") == "clarification":
                clarification_round += 1
                if interaction.get("status") == "resolved":
                    clarification_resolved = True
    verification_items = verification.get("items") if isinstance(verification, Mapping) else None
    delivery_contract_passed = None
    if isinstance(verification_items, list):
        for item in verification_items:
            if not isinstance(item, Mapping):
                continue
            if item.get("label") == "file_changes":
                if item.get("status") == "failed" and item.get("reason_code") in {
                    "incomplete_delivery",
                    "scope_mismatch",
                }:
                    delivery_contract_passed = False
                elif item.get("status") in {"passed", "warning"}:
                    delivery_contract_passed = True

    summary: dict[str, Any] = {
        "current_step": payload.get("current_step"),
        "attempt_count": payload.get("attempt_count"),
        "timeline_persisted_count": payload.get("timeline_persisted_count"),
        "repair_handoff_requested": payload.get("repair_handoff_requested"),
        "result_status": result.get("status"),
        "review_outcome": review.get("outcome"),
        "verification_status": verification.get("status"),
        "verifier_failure_kind": verification.get("failure_kind"),
        "clarification_round": clarification_round or None,
        "clarification_resolved": clarification_resolved if clarification_round else None,
        "delivery_contract_passed": delivery_contract_passed,
        "error_count": (
            len(payload.get("errors", [])) if isinstance(payload.get("errors"), list) else None
        ),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _normalize_requested_permission(raw_permission: object, *, warning_context: str) -> str | None:
    """Normalize a permission value while logging unknown levels."""
    requested_permission_level = coerce_permission_level(raw_permission)
    if raw_permission is not None and requested_permission_level is None:
        logger.warning(
            f"Ignoring unknown permission level from {warning_context}.",
            extra={"requested_permission": raw_permission},
        )
    return requested_permission_level.value if requested_permission_level is not None else None


def _normalize_orchestrator_graph_output(raw_output: object) -> object:
    """Strip transport-only interrupt keys and map unresolved interrupts to failure output."""

    if isinstance(raw_output, Mapping):
        normalized = dict(raw_output)
    elif isinstance(raw_output, BaseModel):
        normalized = raw_output.model_dump(mode="json")
        model_extra = raw_output.model_extra
        if (
            normalized.get("__interrupt__") is None
            and isinstance(model_extra, Mapping)
            and "__interrupt__" in model_extra
        ):
            normalized["__interrupt__"] = model_extra["__interrupt__"]
        if normalized.get("__interrupt__") is None and hasattr(raw_output, "__interrupt__"):
            normalized["__interrupt__"] = getattr(raw_output, "__interrupt__")
    else:
        return raw_output

    existing_result = normalized.get("result")
    normalized_result: dict[str, Any] | None = None
    if isinstance(existing_result, Mapping):
        normalized_result = dict(existing_result)
    elif isinstance(existing_result, BaseModel):
        normalized_result = existing_result.model_dump(mode="json")

    if normalized_result is not None:
        normalized_result["requested_permission"] = _normalize_requested_permission(
            normalized_result.get("requested_permission"),
            warning_context="result payload",
        )
        normalized["result"] = normalized_result

    interrupts_raw = normalized.pop("__interrupt__", None)
    if interrupts_raw is None:
        return normalized

    interrupts = list(interrupts_raw) if isinstance(interrupts_raw, list) else [interrupts_raw]
    payloads = [
        payload
        for payload in (_interrupt_payload_from_object(interrupt) for interrupt in interrupts)
        if payload is not None
    ]
    logger.warning(
        "Orchestrator graph returned unresolved interrupts; normalizing result for persistence.",
        extra={"interrupt_count": len(payloads) or len(interrupts)},
    )

    existing_errors = normalized.get("errors")
    errors = [str(item) for item in existing_errors] if isinstance(existing_errors, list) else []
    errors.append("orchestrator interrupted awaiting manual approval")
    normalized["errors"] = errors

    existing_progress = normalized.get("progress_updates")
    progress_updates = (
        [str(item) for item in existing_progress] if isinstance(existing_progress, list) else []
    )
    progress_updates.append("run interrupted pending manual approval")
    normalized["progress_updates"] = progress_updates

    if normalized.get("result") is None:
        first_payload = payloads[0] if payloads else {}
        requested_permission = _normalize_requested_permission(
            first_payload.get("requested_permission"),
            warning_context="interrupt payload",
        )
        normalized["result"] = WorkerResult(
            status="failure",
            failure_kind="interaction",
            summary=_interrupt_summary(payloads),
            requested_permission=requested_permission,
            commands_run=[],
            files_changed=[],
            test_results=[],
            artifacts=[],
            next_action_hint="await_manual_follow_up",
        ).model_dump(mode="json")
    return normalized


def _requires_manual_follow_up(state: OrchestratorState) -> bool:
    """Return True when a failed result should remain terminal for operator action."""
    if state.result is None:
        return False
    return state.result.next_action_hint == "await_manual_follow_up"


def _terminal_follow_up_status(
    *,
    state: OrchestratorState,
    terminal_failure: bool,
) -> TaskStatus:
    """Map terminal follow-up intent to the persisted task status."""
    if not terminal_failure:
        return TaskStatus.IN_PROGRESS
    if state.approval.status == "rejected":
        return TaskStatus.FAILED
    is_clarification_gate = state.current_step in {
        "generate_task_spec",
        "generate_task_spec_and_route",
        "decompose_task",
        "await_clarification",
    }
    if (
        state.approval.status == "pending"
        or (is_clarification_gate and state.task_spec and state.task_spec.requires_clarification)
        or state.current_step
        in {"await_clarification", "await_permission", "await_permission_escalation"}
    ):
        return TaskStatus.PENDING
    return TaskStatus.FAILED


def _completion_progress_phase(task_snapshot: TaskSnapshot) -> ProgressPhase:
    """Map final task state to a user-facing progress phase."""
    if task_snapshot.status == TaskStatus.COMPLETED.value:
        return "completed"
    if (
        task_snapshot.status == TaskStatus.PENDING.value
        and task_snapshot.approval_status == "pending"
    ):
        return "awaiting_approval"
    return "failed"


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
