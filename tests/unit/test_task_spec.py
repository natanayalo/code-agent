"""Unit tests for deterministic TaskSpec generation."""

from __future__ import annotations

from orchestrator.task_spec import build_task_spec, validate_task_spec_policy


def test_build_task_spec_for_simple_feature_task() -> None:
    """Straightforward implementation tasks produce a low-risk workspace spec."""
    spec = build_task_spec(
        task_text="Add a dashboard filter",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="master",
    )

    assert spec.goal == "Add a dashboard filter"
    assert spec.task_type == "feature"
    assert spec.risk_level == "low"
    assert spec.delivery_mode == "workspace"
    assert spec.requires_permission is False
    assert spec.requires_clarification is False
    assert validate_task_spec_policy(spec) == []


def test_build_task_spec_flags_ambiguous_task_for_clarification() -> None:
    """Underspecified ambiguous asks should surface precise clarification needs."""
    spec = build_task_spec(
        task_text="Analyze",
        repo_url=None,
        target_branch=None,
        task_kind="ambiguous",
    )

    assert spec.task_type == "investigation"
    assert spec.requires_clarification is True
    assert spec.clarification_questions == [
        "What exact repo, files, behavior, or failure should the worker target?"
    ]
    assert "No repository URL was provided" in spec.assumptions[0]
    assert validate_task_spec_policy(spec) == []


def test_build_task_spec_requires_permission_for_destructive_task() -> None:
    """Destructive work is gated before execution policy can allow it."""
    spec = build_task_spec(
        task_text="Delete all generated artifacts and run git clean",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="master",
        constraints={"approval_reason": "Deletes workspace files"},
    )

    assert spec.risk_level == "high"
    assert spec.requires_permission is True
    assert spec.permission_reason == "Deletes workspace files"
    assert "destructive_actions_without_permission" in spec.forbidden_actions
    assert validate_task_spec_policy(spec) == []


def test_build_task_spec_marks_complex_refactor_as_medium_risk() -> None:
    """Complex multi-file refactors should be visible to routing and operator UI."""
    spec = build_task_spec(
        task_text="Refactor orchestration across files",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="task/refactor",
        task_kind="architecture",
        constraints={"delivery_mode": "draft_pr"},
    )

    assert spec.task_type == "refactor"
    assert spec.risk_level == "medium"
    assert spec.delivery_mode == "draft_pr"
    assert "draft_pr_link" in spec.expected_artifacts
    assert spec.requires_permission is False
    assert validate_task_spec_policy(spec) == []
