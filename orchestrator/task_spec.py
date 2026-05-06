"""Deterministic TaskSpec generation and policy validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from orchestrator.brain import TaskSpecBrainMergeReport, TaskSpecBrainSuggestion
from orchestrator.constants import (
    AMBIGUOUS_ASKS,
    BUGFIX_MARKERS,
    CRITICAL_MARKERS,
    DESTRUCTIVE_TASK_MARKERS,
    DOCS_MARKERS,
    HIGH_RISK_MARKERS,
    INVESTIGATION_MARKERS,
    MAINTENANCE_MARKERS,
    REFACTOR_MARKERS,
    REVIEW_FIX_MARKERS,
    RISK_ORDER,
    VALID_DELIVERY_MODES,
)
from orchestrator.state import (
    TaskDeliveryMode,
    TaskPlan,
    TaskRequest,
    TaskRiskLevel,
    TaskSpec,
    TaskSpecType,
)


def _normalized_text(text: str) -> str:
    """Collapse user text into a single-line goal."""
    return " ".join(text.split())


def contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    """Check if any of the markers are present in the text with word boundaries."""
    if not markers:
        return False
    pattern = rf"\b(?:{'|'.join(re.escape(m) for m in markers)})\b"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _coerce_string_list(value: object) -> list[str]:
    """Accept only non-empty string lists from caller-supplied policy fields."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    """Return unique values while preserving first-seen ordering."""
    return list(dict.fromkeys(values))


def _append_unique(base: list[str], additions: list[str]) -> tuple[list[str], list[str]]:
    """Append non-empty additions while reporting only newly inserted values."""
    merged = list(base)
    added: list[str] = []
    for value in additions:
        if value not in merged:
            merged.append(value)
            added.append(value)
    return merged, added


def _resolve_task_type(task_text: str, task_kind: str | None) -> TaskSpecType:
    """Map raw task wording and coarse classification into the TaskSpec vocabulary."""
    if contains_marker(task_text, DOCS_MARKERS):
        return "docs"
    if contains_marker(task_text, REVIEW_FIX_MARKERS):
        return "review_fix"
    if contains_marker(task_text, REFACTOR_MARKERS) or task_kind == "architecture":
        return "refactor"
    if contains_marker(task_text, INVESTIGATION_MARKERS) or task_kind == "ambiguous":
        return "investigation"
    if contains_marker(task_text, BUGFIX_MARKERS):
        return "bugfix"
    if contains_marker(task_text, MAINTENANCE_MARKERS):
        return "maintenance"
    return "feature"


def _max_risk(*levels: str) -> str:
    """Return the highest recognized risk level from the provided candidates."""
    recognized = [level for level in levels if level in RISK_ORDER]
    if not recognized:
        return "low"
    return max(recognized, key=lambda level: RISK_ORDER[level])


def is_destructive_task(task_text: str, constraints: Mapping[str, Any]) -> bool:
    """Return whether the task involves potentially destructive changes."""
    if constraints.get("destructive_action") is True:
        return True
    return contains_marker(task_text, DESTRUCTIVE_TASK_MARKERS)


def _resolve_risk_level(
    *,
    task_text: str,
    constraints: Mapping[str, Any],
    task_type: TaskSpecType,
    task_plan: TaskPlan | None,
) -> TaskRiskLevel:
    """Apply deterministic risk classification with explicit constraints as an upper hint."""
    explicit_risk = constraints.get("risk_level")
    explicit = explicit_risk.strip().lower() if isinstance(explicit_risk, str) else "low"
    risk = "low"

    destructive = is_destructive_task(task_text, constraints)
    if destructive or contains_marker(task_text, HIGH_RISK_MARKERS):
        risk = _max_risk(risk, "high")
    if contains_marker(task_text, CRITICAL_MARKERS):
        risk = _max_risk(risk, "critical")
    if task_type in {"refactor", "review_fix"} or (task_plan is not None and task_plan.triggered):
        risk = _max_risk(risk, "medium")

    return cast(TaskRiskLevel, _max_risk(risk, explicit))


def _resolve_delivery_mode(task_text: str, constraints: Mapping[str, Any]) -> TaskDeliveryMode:
    """Resolve desired delivery without claiming the current runtime can perform it yet."""
    explicit_delivery_mode = constraints.get("delivery_mode")
    if isinstance(explicit_delivery_mode, str):
        normalized = explicit_delivery_mode.strip().lower()
        if normalized in VALID_DELIVERY_MODES:
            return cast(TaskDeliveryMode, normalized)

    if contains_marker(task_text, ("draft pr", "pull request", "pr")):
        return "draft_pr"
    if contains_marker(task_text, ("branch",)):
        return "branch"
    if contains_marker(task_text, ("summary only",)):
        return "summary"
    return "workspace"


def _requires_clarification(task_text: str, task_kind: str | None) -> bool:
    """Flag raw asks that are too underspecified for reliable autonomous execution."""
    normalized = task_text.lower().strip()
    if len(normalized.split()) <= 2 and task_kind == "ambiguous":
        return True
    return contains_marker(normalized, AMBIGUOUS_ASKS)


def build_task_spec(
    *,
    task_text: str,
    repo_url: str | None,
    target_branch: str | None,
    constraints: Mapping[str, Any] | None = None,
    task_kind: str | None = None,
    task_plan: TaskPlan | None = None,
) -> TaskSpec:
    """Build the persisted structured task contract for a raw task request."""
    normalized_task_text = _normalized_text(task_text)
    task_constraints: Mapping[str, Any] = constraints or {}
    task_type = _resolve_task_type(normalized_task_text, task_kind)
    risk_level = _resolve_risk_level(
        task_text=normalized_task_text,
        constraints=task_constraints,
        task_type=task_type,
        task_plan=task_plan,
    )
    delivery_mode = _resolve_delivery_mode(normalized_task_text, task_constraints)

    assumptions = _coerce_string_list(task_constraints.get("assumptions"))
    if not repo_url:
        assumptions.append("No repository URL was provided; use the configured workspace context.")
    assumptions.append("Prefer the smallest safe implementation slice.")

    acceptance_criteria = _coerce_string_list(task_constraints.get("acceptance_criteria"))
    if not acceptance_criteria:
        acceptance_criteria = [
            "Requested goal is implemented or conclusively reported as blocked.",
            "Relevant focused verification commands are run or a clear reason is recorded.",
        ]

    non_goals = _coerce_string_list(task_constraints.get("non_goals"))
    non_goals.extend(
        [
            "Do not make unrelated refactors.",
            (
                "Do not change auth, secrets, billing, sandbox policy, or deployment "
                "permissions without explicit approval."
            ),
        ]
    )

    allowed_actions = [
        "read_repo_files",
        "modify_workspace_files",
        "run_non_destructive_checks",
    ]
    if delivery_mode in {"branch", "draft_pr"}:
        allowed_actions.append("prepare_branch_delivery")
    if delivery_mode == "draft_pr":
        allowed_actions.append("prepare_draft_pr_delivery")

    forbidden_actions = [
        "hardcode_secrets",
        "destructive_git_operations",
        "deploy_or_merge_without_approval",
    ]

    requires_permission = risk_level in {"high", "critical"} or (
        task_constraints.get("requires_approval") is True
    )
    permission_reason = None
    if requires_permission:
        forbidden_actions.append("destructive_actions_without_permission")
        explicit_reason = task_constraints.get("approval_reason")
        permission_reason = (
            explicit_reason.strip()
            if isinstance(explicit_reason, str) and explicit_reason.strip()
            else f"Task is classified as {risk_level} risk."
            if risk_level in {"high", "critical"}
            else "Manual approval required for this task."
        )

    requires_clarification = _requires_clarification(normalized_task_text, task_kind)
    clarification_questions = _coerce_string_list(task_constraints.get("clarification_questions"))
    if requires_clarification and not clarification_questions:
        clarification_questions = [
            "What exact repo, files, behavior, or failure should the worker target?"
        ]

    expected_artifacts = ["summary"]
    if delivery_mode != "summary":
        expected_artifacts.append("workspace_diff")
    if delivery_mode in {"branch", "draft_pr"}:
        expected_artifacts.append("branch_reference")
        if delivery_mode == "draft_pr":
            expected_artifacts.append("draft_pr_link")

    return TaskSpec(
        goal=normalized_task_text,
        repo_url=repo_url,
        target_branch=target_branch,
        assumptions=list(dict.fromkeys(assumptions)),
        acceptance_criteria=list(dict.fromkeys(acceptance_criteria)),
        non_goals=list(dict.fromkeys(non_goals)),
        risk_level=risk_level,
        task_type=task_type,
        allowed_actions=allowed_actions,
        forbidden_actions=list(dict.fromkeys(forbidden_actions)),
        verification_commands=_coerce_string_list(task_constraints.get("verification_commands")),
        expected_artifacts=expected_artifacts,
        requires_clarification=requires_clarification,
        clarification_questions=list(dict.fromkeys(clarification_questions)),
        requires_permission=requires_permission,
        permission_reason=permission_reason,
        delivery_mode=delivery_mode,
    )


def build_task_spec_for_request(
    task: TaskRequest,
    *,
    task_kind: str | None,
    task_plan: TaskPlan | None,
) -> TaskSpec:
    """Build a TaskSpec from the orchestrator's normalized task request model."""
    return build_task_spec(
        task_text=task.task_text,
        repo_url=task.repo_url,
        target_branch=task.branch,
        constraints=task.constraints,
        task_kind=task_kind,
        task_plan=task_plan,
    )


def apply_task_spec_brain_suggestion(
    *,
    task_spec: TaskSpec,
    suggestion: TaskSpecBrainSuggestion,
    provider: str | None = None,
) -> tuple[TaskSpec, TaskSpecBrainMergeReport]:
    """Safely merge optional brain suggestions into a deterministic TaskSpec."""
    assumptions, added_assumptions = _append_unique(
        list(task_spec.assumptions),
        _coerce_string_list(suggestion.assumptions),
    )
    acceptance_criteria, added_acceptance = _append_unique(
        list(task_spec.acceptance_criteria),
        _coerce_string_list(suggestion.acceptance_criteria),
    )
    non_goals, added_non_goals = _append_unique(
        list(task_spec.non_goals),
        _coerce_string_list(suggestion.non_goals),
    )
    clarification_questions, added_clarification_questions = _append_unique(
        list(task_spec.clarification_questions),
        _coerce_string_list(suggestion.clarification_questions),
    )

    ignored_fields: list[str] = []
    suggested_task_type = suggestion.suggested_task_type
    if suggested_task_type is not None and suggested_task_type != task_spec.task_type:
        ignored_fields.append("suggested_task_type")

    suggested_delivery_mode = suggestion.suggested_delivery_mode
    if suggested_delivery_mode is not None and suggested_delivery_mode != task_spec.delivery_mode:
        ignored_fields.append("suggested_delivery_mode")

    risk_level = task_spec.risk_level
    suggested_risk_level = suggestion.suggested_risk_level
    if suggested_risk_level is not None:
        if RISK_ORDER[suggested_risk_level] > RISK_ORDER[risk_level]:
            risk_level = cast(TaskRiskLevel, suggested_risk_level)
        elif RISK_ORDER[suggested_risk_level] < RISK_ORDER[risk_level]:
            ignored_fields.append("suggested_risk_level")

    requires_permission = task_spec.requires_permission or risk_level in {"high", "critical"}
    permission_reason = task_spec.permission_reason
    forbidden_actions = _dedupe_preserving_order(list(task_spec.forbidden_actions))
    if requires_permission:
        if "destructive_actions_without_permission" not in forbidden_actions:
            forbidden_actions.append("destructive_actions_without_permission")
        if not permission_reason:
            permission_reason = f"Task is classified as {risk_level} risk."

    requires_clarification = task_spec.requires_clarification or bool(clarification_questions)

    merged_spec = task_spec.model_copy(
        update={
            "assumptions": assumptions,
            "acceptance_criteria": acceptance_criteria,
            "non_goals": non_goals,
            "clarification_questions": clarification_questions,
            "requires_clarification": requires_clarification,
            "risk_level": risk_level,
            "requires_permission": requires_permission,
            "permission_reason": permission_reason,
            "forbidden_actions": forbidden_actions,
        }
    )

    report = TaskSpecBrainMergeReport(
        enabled=True,
        provider=provider,
        applied=bool(
            added_assumptions
            or added_acceptance
            or added_non_goals
            or added_clarification_questions
            or risk_level != task_spec.risk_level
            or requires_permission != task_spec.requires_permission
            or permission_reason != task_spec.permission_reason
            or forbidden_actions != task_spec.forbidden_actions
        ),
        added_assumptions=added_assumptions,
        added_acceptance_criteria=added_acceptance,
        added_non_goals=added_non_goals,
        added_clarification_questions=added_clarification_questions,
        ignored_fields=_dedupe_preserving_order(ignored_fields),
        rationale=suggestion.rationale,
    )

    return merged_spec, report


def validate_task_spec_policy(task_spec: TaskSpec) -> list[str]:
    """Return deterministic policy violations for a generated TaskSpec."""
    violations: list[str] = []
    if task_spec.requires_clarification and not task_spec.clarification_questions:
        violations.append("clarification_required_without_questions")
    if task_spec.risk_level in {"high", "critical"} and not task_spec.requires_permission:
        violations.append("high_risk_without_permission_gate")
    if task_spec.requires_permission and not task_spec.permission_reason:
        violations.append("permission_required_without_reason")
    if "hardcode_secrets" not in task_spec.forbidden_actions:
        violations.append("missing_secret_hardcode_forbidden_action")
    return violations
