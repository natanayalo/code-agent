"""Deterministic TaskSpec generation and policy validation."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from orchestrator.brain import TaskSpecBrainMergeReport, TaskSpecBrainSuggestion
from orchestrator.repo_profile import RepoProfile

if TYPE_CHECKING:
    from orchestrator.repo_profile import RepoProfile
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
from orchestrator.nodes.utils import _dedupe_preserving_order
from orchestrator.state import (
    TaskDeliveryMode,
    TaskPlan,
    TaskRequest,
    TaskRiskLevel,
    TaskSpec,
    TaskSpecType,
)

MAX_CLARIFICATION_QUESTIONS = 3


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


def _append_unique(base: list[str], additions: list[str]) -> tuple[list[str], list[str]]:
    """Append non-empty additions while reporting only newly inserted values."""
    merged = list(base)
    added: list[str] = []
    for value in additions:
        if value not in merged:
            merged.append(value)
            added.append(value)
    return merged, added


def _normalize_clarification_questions(questions: list[str]) -> list[str]:
    """Trim, dedupe, and cap clarification questions to avoid operator overload."""
    return _dedupe_preserving_order(_coerce_string_list(questions))[:MAX_CLARIFICATION_QUESTIONS]


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


def _is_pwd_home_smoke_task(task_text: str) -> bool:
    """Detect the common read-only environment smoke check used by operators."""
    normalized = task_text.lower()
    has_env_targets = (
        re.search(r"\bpwd\b", normalized) is not None
        and re.search(r"\bhome\b", normalized) is not None
    )
    if not has_env_targets:
        return False
    asks_to_print = contains_marker(normalized, ("print", "echo", "show"))
    bounded_to_check = (
        "smoke test" in normalized or "then exit" in normalized or "only" in normalized
    )
    return asks_to_print and bounded_to_check


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
    if task_constraints.get("task_type") == "scout":
        task_type = cast(TaskSpecType, "scout")
    else:
        task_type = _resolve_task_type(normalized_task_text, task_kind)
    is_no_modification_smoke = (
        _is_pwd_home_smoke_task(normalized_task_text)
        or task_constraints.get("read_only") is True
        or task_type == "scout"
    )
    if _is_pwd_home_smoke_task(normalized_task_text) and task_type != "scout":
        task_type = "maintenance"
    risk_level = _resolve_risk_level(
        task_text=normalized_task_text,
        constraints=task_constraints,
        task_type=task_type,
        task_plan=task_plan,
    )
    delivery_mode = _resolve_delivery_mode(normalized_task_text, task_constraints)
    if is_no_modification_smoke and delivery_mode == "workspace":
        delivery_mode = "summary"
    if task_type == "scout":
        delivery_mode = cast(TaskDeliveryMode, "summary")

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
            *(["Do not create or modify any files."] if is_no_modification_smoke else []),
            (
                "Do not change auth, secrets, billing, sandbox policy, or deployment "
                "permissions without explicit approval."
            ),
        ]
    )

    allowed_actions = [
        "read_repo_files",
        "run_non_destructive_checks",
    ]
    if not is_no_modification_smoke:
        allowed_actions.insert(1, "modify_workspace_files")
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
    clarification_questions = _normalize_clarification_questions(
        _coerce_string_list(task_constraints.get("clarification_questions"))
    )
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

    delivery_branch = task_constraints.get("delivery_branch")
    if not isinstance(delivery_branch, str) or not delivery_branch.strip():
        delivery_branch = None
    pr_title = task_constraints.get("pr_title")
    if not isinstance(pr_title, str) or not pr_title.strip():
        pr_title = None
    pr_body = task_constraints.get("pr_body")
    if not isinstance(pr_body, str) or not pr_body.strip():
        pr_body = None

    workspace_mode_raw = task_constraints.get("workspace_mode")
    workspace_mode = "clone"
    if workspace_mode_raw in ("clone", "init", "none"):
        workspace_mode = workspace_mode_raw

    return TaskSpec(
        goal=normalized_task_text,
        repo_url=repo_url,
        target_branch=target_branch,
        workspace_mode=workspace_mode,  # type: ignore
        assumptions=list(dict.fromkeys(assumptions)),
        acceptance_criteria=list(dict.fromkeys(acceptance_criteria)),
        non_goals=list(dict.fromkeys(non_goals)),
        risk_level=risk_level,
        task_type=task_type,
        allowed_actions=allowed_actions,
        forbidden_actions=list(dict.fromkeys(forbidden_actions)),
        verification_commands=(
            _coerce_string_list(task_constraints.get("verification_commands"))
            or (['printf \'%s\\n%s\\n\' "$PWD" "$HOME"'] if is_no_modification_smoke else [])
        ),
        expected_artifacts=expected_artifacts,
        requires_clarification=requires_clarification,
        clarification_questions=clarification_questions,
        requires_permission=requires_permission,
        permission_reason=permission_reason,
        delivery_mode=delivery_mode,
        delivery_branch=delivery_branch,
        pr_title=pr_title,
        pr_body=pr_body,
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
    clarification_questions = _normalize_clarification_questions(clarification_questions)
    added_clarification_questions = [
        question
        for question in added_clarification_questions
        if question in clarification_questions
    ]
    verification_commands, added_verification_commands = _append_unique(
        list(task_spec.verification_commands),
        _coerce_string_list(suggestion.verification_commands),
    )

    ignored_fields: list[str] = []
    suggested_task_type = suggestion.suggested_task_type
    if suggested_task_type is not None and suggested_task_type != task_spec.task_type:
        ignored_fields.append("suggested_task_type")

    suggested_delivery_mode = suggestion.suggested_delivery_mode
    if suggested_delivery_mode is not None and suggested_delivery_mode != task_spec.delivery_mode:
        ignored_fields.append("suggested_delivery_mode")

    delivery_branch = task_spec.delivery_branch
    suggested_delivery_branch = suggestion.suggested_delivery_branch
    if suggested_delivery_branch is not None and suggested_delivery_branch.strip():
        suggested_delivery_branch = suggested_delivery_branch.strip()
        if not delivery_branch:
            delivery_branch = suggested_delivery_branch
        elif suggested_delivery_branch != delivery_branch:
            ignored_fields.append("suggested_delivery_branch")

    pr_title = task_spec.pr_title
    suggested_pr_title = suggestion.suggested_pr_title
    if suggested_pr_title is not None and suggested_pr_title.strip():
        suggested_pr_title = suggested_pr_title.strip()
        if not pr_title:
            pr_title = suggested_pr_title
        elif suggested_pr_title != pr_title:
            ignored_fields.append("suggested_pr_title")

    pr_body = task_spec.pr_body
    suggested_pr_body = suggestion.suggested_pr_body
    if suggested_pr_body is not None and suggested_pr_body.strip():
        suggested_pr_body = suggested_pr_body.strip()
        if not pr_body:
            pr_body = suggested_pr_body
        elif suggested_pr_body != pr_body:
            ignored_fields.append("suggested_pr_body")

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
            "verification_commands": verification_commands,
            "requires_clarification": requires_clarification,
            "risk_level": risk_level,
            "requires_permission": requires_permission,
            "permission_reason": permission_reason,
            "forbidden_actions": forbidden_actions,
            "delivery_branch": delivery_branch,
            "pr_title": pr_title,
            "pr_body": pr_body,
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
            or added_verification_commands
            or risk_level != task_spec.risk_level
            or requires_permission != task_spec.requires_permission
            or permission_reason != task_spec.permission_reason
            or forbidden_actions != task_spec.forbidden_actions
            or delivery_branch != task_spec.delivery_branch
            or pr_title != task_spec.pr_title
            or pr_body != task_spec.pr_body
        ),
        added_assumptions=added_assumptions,
        added_acceptance_criteria=added_acceptance,
        added_non_goals=added_non_goals,
        added_clarification_questions=added_clarification_questions,
        added_verification_commands=added_verification_commands,
        ignored_fields=_dedupe_preserving_order(ignored_fields),
        rationale=suggestion.rationale,
    )

    return merged_spec, report


def apply_repo_profile_to_task_spec(
    task_spec: TaskSpec,
    repo_profile: RepoProfile | None,
    task_plan: TaskPlan | None = None,
) -> TaskSpec:
    """Apply the deterministic repo profile overlay to an existing TaskSpec."""
    if not repo_profile:
        return task_spec

    setup_commands = (
        list(repo_profile.setup.commands)
        if repo_profile.setup.commands
        else list(task_spec.setup_commands)
    )

    needs_escalation, escalation_reason = _check_task_text_for_escalation(
        task_spec, repo_profile, task_plan
    )

    risk_level = task_spec.risk_level
    requires_permission = task_spec.requires_permission
    permission_reason = task_spec.permission_reason
    forbidden_actions = list(task_spec.forbidden_actions)

    if needs_escalation:
        risk_level = cast(TaskRiskLevel, _max_risk(risk_level, "high"))
        requires_permission = True
        permission_reason = escalation_reason or "Repo profile dictates high risk."
        if "destructive_actions_without_permission" not in forbidden_actions:
            forbidden_actions.append("destructive_actions_without_permission")

    # Assign verification commands based on risk level
    verification_commands = list(task_spec.verification_commands)
    # Only override if empty or if it's the default read-only smoke check
    is_default_smoke = (
        len(verification_commands) == 1
        and "printf" in verification_commands[0]
        and "$PWD" in verification_commands[0]
    )

    if not verification_commands or is_default_smoke:
        if RISK_ORDER[risk_level] >= RISK_ORDER["medium"] and repo_profile.validation.full:
            verification_commands = list(repo_profile.validation.full)
        elif repo_profile.validation.quick:
            verification_commands = list(repo_profile.validation.quick)

    delivery_mode = task_spec.delivery_mode
    if delivery_mode == "workspace" and repo_profile.delivery.default_mode != "workspace":
        delivery_mode = repo_profile.delivery.default_mode

    return task_spec.model_copy(
        update={
            "setup_commands": setup_commands,
            "risk_level": risk_level,
            "requires_permission": requires_permission,
            "permission_reason": permission_reason,
            "forbidden_actions": forbidden_actions,
            "verification_commands": verification_commands,
            "delivery_mode": delivery_mode,
        }
    )


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


def _check_task_text_for_escalation(
    task_spec: TaskSpec, repo_profile: RepoProfile, task_plan: TaskPlan | None
) -> tuple[bool, str | None]:
    """Check task text against repo profile policies for required escalation."""
    text_to_check = task_spec.goal.lower()
    if task_plan and task_plan.steps:
        for step in task_plan.steps:
            text_to_check += " " + step.title.lower() + " " + step.expected_outcome.lower()

    if repo_profile.protected_paths:
        patterns = []
        for protected_path in repo_profile.protected_paths:
            normalized_path = protected_path.strip().lower()
            if not normalized_path:
                continue
            pattern = fnmatch.translate(normalized_path).removeprefix("(?s:").removesuffix(")\\Z")
            if re.match(r"^\w", normalized_path):
                pattern = r"(?<!\w)" + pattern
            if re.search(r"\w$", normalized_path):
                pattern = pattern + r"(?!\w)"
            patterns.append(pattern)
        if patterns:
            combined_regex = re.compile("|".join(patterns), re.IGNORECASE)
            if combined_regex.search(text_to_check):
                return True, "Task may affect protected paths"

    for category in repo_profile.approval_required:
        cat_word = category.replace("_", " ").lower()
        if cat_word in text_to_check:
            return True, f"Task may involve approval-required category: {category}"

    return False, None
