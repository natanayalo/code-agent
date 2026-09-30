"""Policy checks for isolated post-terminal quality evaluation tasks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestrator.state import TaskSpec


def post_terminal_quality_evaluation_spec_errors(
    task_spec: TaskSpec | None,
    constraints: Mapping[str, Any],
) -> list[str]:
    """Return persisted TaskSpec violations that must block evaluation dispatch."""
    if task_spec is None:
        return ["missing_task_spec"]

    errors: list[str] = []
    if task_spec.task_type != "feature":
        errors.append("task_type_must_be_feature")
    if "modify_workspace_files" not in task_spec.allowed_actions:
        errors.append("workspace_mutation_action_required")
    if task_spec.delivery_mode != "workspace":
        errors.append("delivery_mode_must_be_workspace")
    if task_spec.delivery_branch is not None:
        errors.append("delivery_branch_must_be_empty")
    if {"prepare_branch_delivery", "prepare_draft_pr_delivery"} & set(task_spec.allowed_actions):
        errors.append("remote_delivery_actions_forbidden")
    if task_spec.verification_commands:
        errors.append("task_verification_commands_must_be_empty")
    if task_spec.requires_clarification or task_spec.clarification_questions:
        errors.append("clarification_must_not_be_required")
    if task_spec.requires_permission:
        errors.append("permission_must_not_be_required")
    if constraints.get("skip_independent_review") is not True:
        errors.append("skip_independent_review_constraint_required")
    return errors
