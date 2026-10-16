"""Isolation gates, stop-condition validators, and cleanup helpers for M29 mutation evaluation."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from db.enums import TimelineEventType
from db.models import HumanInteraction, TaskTimelineEvent
from evaluation.m29_mutation_suite import M29MutationCase
from orchestrator.post_terminal_quality import post_terminal_quality_evaluation_spec_errors
from orchestrator.state import MemoryContext, TaskSpec
from sandbox.workspace import (
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
)

LOGGER = logging.getLogger("m29_mutation_isolation")

MAX_EVALUATION_RETENTION_SECONDS: int = 259_200  # 72 hours

DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    "evaluation/",
    "tests/",
    "orchestrator/",
    "sandbox/",
    "workers/",
    "db/",
    "repositories/",
    "apps/",
    "pyproject.toml",
    "poetry.lock",
    ".git",
    ".github/",
    ".agents/",
)

INTERACTION_STOP_EVENT_TYPES: frozenset[TimelineEventType] = frozenset(
    {
        TimelineEventType.APPROVAL_REQUESTED,
        TimelineEventType.INTERACTION_RESOLVED,
    }
)

DELIVERY_STOP_EVENT_TYPES: frozenset[TimelineEventType] = frozenset(
    {
        TimelineEventType.DELIVERY_STARTED,
        TimelineEventType.DELIVERY_COMPLETED,
        TimelineEventType.DELIVERY_FAILED,
    }
)


def compute_memory_context_hash(memory_context: MemoryContext) -> str:
    """Compute deterministic SHA-256 hash of serialized worker-visible MemoryContext."""
    raw = memory_context.model_dump(mode="json")
    canonical_json = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def verify_memory_context_isolation(
    memory_context: MemoryContext,
    baseline_hash: str,
    *,
    require_empty_personal: bool = True,
) -> list[str]:
    """Verify worker-visible MemoryContext matches frozen baseline and preserves isolation."""
    errors: list[str] = []
    if require_empty_personal and len(memory_context.personal) > 0:
        errors.append("personal_memory_store_not_empty")

    actual_hash = compute_memory_context_hash(memory_context)
    if actual_hash != baseline_hash:
        errors.append(f"memory_context_hash_mismatch:{actual_hash}!={baseline_hash}")
    return errors


def validate_post_overlay_task_spec(
    task_spec: TaskSpec | Mapping[str, Any] | None,
    constraints: Mapping[str, Any],
    *,
    expected_profile: str | None = None,
    actual_profile: str | None = None,
    expected_fixture_revision: str | None = None,
    actual_fixture_revision: str | None = None,
) -> list[str]:
    """Validate finalized post-overlay TaskSpec, profile, and fixture revision before dispatch."""
    if task_spec is None:
        return ["missing_task_spec"]

    spec_model: TaskSpec
    if isinstance(task_spec, TaskSpec):
        spec_model = task_spec
    elif isinstance(task_spec, Mapping):
        try:
            spec_model = TaskSpec.model_validate(task_spec)
        except Exception as exc:
            return [f"invalid_task_spec:{exc}"]
    else:
        return ["invalid_task_spec_type"]

    errors = post_terminal_quality_evaluation_spec_errors(spec_model, constraints)

    if expected_profile is not None and actual_profile != expected_profile:
        errors.append(f"worker_profile_mismatch:{actual_profile}!={expected_profile}")

    if (
        expected_fixture_revision is not None
        and actual_fixture_revision != expected_fixture_revision
    ):
        errors.append(
            f"fixture_revision_mismatch:{actual_fixture_revision}!={expected_fixture_revision}"
        )
    return errors


def _extract_comparable_spec_dict(spec: TaskSpec | Mapping[str, Any]) -> dict[str, Any]:
    d = spec.model_dump(mode="json") if isinstance(spec, TaskSpec) else dict(spec)
    return {
        "task_type": d.get("task_type"),
        "allowed_actions": sorted(d.get("allowed_actions") or []),
        "setup_commands": d.get("setup_commands") or [],
        "verification_commands": d.get("verification_commands") or [],
        "delivery_mode": d.get("delivery_mode"),
        "delivery_branch": d.get("delivery_branch"),
        "requires_clarification": bool(d.get("requires_clarification")),
        "clarification_questions": d.get("clarification_questions") or [],
        "requires_permission": bool(d.get("requires_permission")),
        "goal": d.get("goal"),
        "acceptance_criteria": d.get("acceptance_criteria") or [],
        "non_goals": d.get("non_goals") or [],
    }


def verify_pair_semantic_equality(
    case_a: M29MutationCase,
    case_b: M29MutationCase,
    task_spec_a: TaskSpec | Mapping[str, Any],
    task_spec_b: TaskSpec | Mapping[str, Any],
    constraints_a: Mapping[str, Any],
    constraints_b: Mapping[str, Any],
    repo_profile_a: Any | None = None,
    repo_profile_b: Any | None = None,
) -> list[str]:
    """Verify evaluation-controlled semantic equality between matched pair cases."""
    errors: list[str] = []

    if case_a.prompt != case_b.prompt:
        errors.append("pair_prompt_mismatch")
    if case_a.acceptance_oracle_sha256 != case_b.acceptance_oracle_sha256:
        errors.append("pair_acceptance_oracle_hash_mismatch")
    if case_a.target_repository_revision != case_b.target_repository_revision:
        errors.append("pair_target_repository_revision_mismatch")
    if case_a.candidate_timeout_seconds != case_b.candidate_timeout_seconds:
        errors.append("pair_candidate_timeout_mismatch")
    if case_a.task_budget != case_b.task_budget:
        errors.append("pair_task_budget_mismatch")
    if case_a.neutral_evaluator != case_b.neutral_evaluator:
        errors.append("pair_neutral_evaluator_mismatch")

    dict_a = _extract_comparable_spec_dict(task_spec_a)
    dict_b = _extract_comparable_spec_dict(task_spec_b)
    if dict_a != dict_b:
        errors.append(f"pair_task_spec_mismatch:{dict_a}!={dict_b}")

    controlled_keys = (
        "skip_independent_review",
        "post_terminal_quality_evaluation",
        "delivery_mode",
    )
    for key in controlled_keys:
        if constraints_a.get(key) != constraints_b.get(key):
            errors.append(f"pair_constraint_mismatch:{key}")

    if repo_profile_a != repo_profile_b:
        errors.append("pair_repo_profile_mismatch")

    return errors


@dataclass(frozen=True)
class TerminalSnapshot:
    """Immutable terminal task snapshot captured before external evaluation."""

    task_status: str
    terminal_event_type: str
    terminal_timestamp: str
    failure_kind: str | None = None
    diff_sha256: str | None = None
    changed_files: tuple[str, ...] = ()
    checksum: str = ""


def compute_terminal_snapshot_checksum(
    task_status: str,
    terminal_event_type: str,
    terminal_timestamp: datetime | str,
    failure_kind: str | None = None,
    diff_sha256: str | None = None,
    changed_files: Sequence[str] = (),
) -> str:
    """Compute deterministic SHA-256 checksum of an immutable terminal snapshot."""
    iso_ts = (
        terminal_timestamp.astimezone(UTC).isoformat()
        if isinstance(terminal_timestamp, datetime)
        else str(terminal_timestamp)
    )
    payload = {
        "task_status": str(task_status),
        "terminal_event_type": str(terminal_event_type),
        "terminal_timestamp": iso_ts,
        "failure_kind": failure_kind,
        "diff_sha256": diff_sha256,
        "changed_files": sorted(changed_files),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def create_terminal_snapshot(
    task_status: str,
    terminal_event_type: str,
    terminal_timestamp: datetime | str,
    failure_kind: str | None = None,
    diff_sha256: str | None = None,
    changed_files: Sequence[str] = (),
) -> TerminalSnapshot:
    """Create a validated TerminalSnapshot with computed checksum."""
    iso_ts = (
        terminal_timestamp.astimezone(UTC).isoformat()
        if isinstance(terminal_timestamp, datetime)
        else str(terminal_timestamp)
    )
    checksum = compute_terminal_snapshot_checksum(
        task_status=task_status,
        terminal_event_type=terminal_event_type,
        terminal_timestamp=iso_ts,
        failure_kind=failure_kind,
        diff_sha256=diff_sha256,
        changed_files=changed_files,
    )
    return TerminalSnapshot(
        task_status=str(task_status),
        terminal_event_type=str(terminal_event_type),
        terminal_timestamp=iso_ts,
        failure_kind=failure_kind,
        diff_sha256=diff_sha256,
        changed_files=tuple(sorted(changed_files)),
        checksum=checksum,
    )


def verify_terminal_snapshot_immutability(
    before: TerminalSnapshot | Mapping[str, Any],
    after: TerminalSnapshot | Mapping[str, Any],
) -> list[str]:
    """Verify that task status, terminal event, diff, and checksum remain strictly immutable."""
    d_before = asdict(before) if isinstance(before, TerminalSnapshot) else dict(before)
    d_after = asdict(after) if isinstance(after, TerminalSnapshot) else dict(after)

    violations: list[str] = []
    for field in (
        "task_status",
        "terminal_event_type",
        "terminal_timestamp",
        "failure_kind",
        "diff_sha256",
        "changed_files",
        "checksum",
    ):
        v_before = d_before.get(field)
        v_after = d_after.get(field)
        if isinstance(v_before, list):
            v_before = tuple(v_before)
        if isinstance(v_after, list):
            v_after = tuple(v_after)
        if v_before != v_after:
            violations.append(f"terminal_snapshot_mutated:{field}:{v_before}!={v_after}")
    return violations


def check_pending_interaction_stop_condition(
    interactions: Sequence[HumanInteraction | Mapping[str, Any]] = (),
    timeline_events: Sequence[TaskTimelineEvent | Mapping[str, Any]] = (),
) -> str | None:
    """Check if task created or is waiting on a human interaction."""
    if interactions:
        return "pending_human_interaction_detected"

    for ev in timeline_events:
        etype = getattr(ev, "event_type", None) or (
            ev.get("event_type") if isinstance(ev, Mapping) else None
        )
        if etype in INTERACTION_STOP_EVENT_TYPES or "interaction" in str(etype).lower():
            return "human_interaction_event_detected"
    return None


def check_delivery_stop_condition(
    task_spec: TaskSpec | Mapping[str, Any] | None,
    timeline_events: Sequence[TaskTimelineEvent | Mapping[str, Any]] = (),
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> str | None:
    """Check if task requested, initiated, or produced remote delivery."""
    if task_spec is not None:
        d_mode = getattr(task_spec, "delivery_mode", None) or (
            task_spec.get("delivery_mode") if isinstance(task_spec, Mapping) else None
        )
        if d_mode != "workspace":
            return "invalid_delivery_mode_not_workspace"

        d_branch = getattr(task_spec, "delivery_branch", None) or (
            task_spec.get("delivery_branch") if isinstance(task_spec, Mapping) else None
        )
        if d_branch is not None:
            return "delivery_branch_forbidden"

        actions = getattr(task_spec, "allowed_actions", None) or (
            task_spec.get("allowed_actions") if isinstance(task_spec, Mapping) else []
        )
        if {"prepare_branch_delivery", "prepare_draft_pr_delivery"} & set(actions):
            return "remote_delivery_actions_forbidden"

    for ev in timeline_events:
        etype = getattr(ev, "event_type", None) or (
            ev.get("event_type") if isinstance(ev, Mapping) else None
        )
        if etype in DELIVERY_STOP_EVENT_TYPES or "delivery" in str(etype).lower():
            return "delivery_event_detected"

    for art in artifacts:
        atype = art.get("artifact_type") or art.get("type")
        if atype in ("pull_request", "delivery", "draft_pr", "branch_delivery"):
            return "delivery_artifact_detected"

    return None


def check_protected_paths_stop_condition(
    changed_files: Sequence[str],
    allowed_source_path: str,
    *,
    protected_paths: Sequence[str] = DEFAULT_PROTECTED_PATHS,
) -> list[str]:
    """Check if task modified protected paths or paths outside allowed source boundary."""
    violations: list[str] = []
    norm_allowed = allowed_source_path.strip("/")

    for file_path in changed_files:
        norm_file = file_path.strip("/")
        for prot in protected_paths:
            norm_prot = prot.strip("/")
            if norm_file == norm_prot or norm_file.startswith(norm_prot + "/"):
                violations.append(f"protected_path_modified:{file_path}")
                break

        if norm_file != norm_allowed and not norm_file.startswith(norm_allowed + "/"):
            violations.append(f"unauthorized_source_path:{file_path}")

    return violations


def validate_evaluation_retention_seconds(retention_seconds: int) -> None:
    """Validate that evaluation workspace retention does not exceed 72 hours."""
    if retention_seconds <= 0:
        raise ValueError("evaluation retention_seconds must be positive")
    if retention_seconds > MAX_EVALUATION_RETENTION_SECONDS:
        raise ValueError(
            f"evaluation retention_seconds {retention_seconds} exceeds maximum "
            f"72 hours ({MAX_EVALUATION_RETENTION_SECONDS}s)"
        )


def build_evaluation_cleanup_policy() -> WorkspaceCleanupPolicy:
    """Return the one-time cleanup policy for post-archival evaluation cleanup."""
    return WorkspaceCleanupPolicy(delete_on_success=True, retain_on_failure=False)


def cleanup_evaluation_workspace(
    workspace_manager: WorkspaceManager,
    handle: WorkspaceHandle,
    *,
    succeeded: bool,
    recorded_workspace_ids: Collection[str] | None = None,
) -> bool:
    """Clean up an evaluation workspace enforcing recorded ID boundaries and delete policy."""
    if recorded_workspace_ids is not None and handle.workspace_id not in recorded_workspace_ids:
        raise WorkspaceManagerError(
            f"workspace '{handle.workspace_id}' is not in recorded evaluation workspace IDs"
        )

    policy_override = build_evaluation_cleanup_policy()
    handle_copy = handle.model_copy(update={"cleanup_policy": policy_override})
    return workspace_manager.cleanup_workspace(handle_copy, succeeded=succeeded)
