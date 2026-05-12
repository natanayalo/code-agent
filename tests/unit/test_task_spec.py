"""Unit tests for deterministic TaskSpec generation."""

from __future__ import annotations

import pytest

from orchestrator.brain import RuleBasedOrchestratorBrain, TaskSpecBrainSuggestion
from orchestrator.state import TaskRequest
from orchestrator.task_spec import (
    apply_task_spec_brain_suggestion,
    build_task_spec,
    validate_task_spec_policy,
)


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


def test_apply_task_spec_brain_suggestion_adds_fields_and_escalates_risk() -> None:
    """Brain enrichment should be additive and may only raise risk, never lower policy safety."""
    spec = build_task_spec(
        task_text="Investigate why CI fails",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
    )
    suggestion = TaskSpecBrainSuggestion(
        assumptions=["CI runners are available."],
        acceptance_criteria=["Include root-cause summary in final output."],
        non_goals=["Do not merge changes automatically."],
        clarification_questions=["Which CI job is failing most often?"],
        verification_commands=["pytest tests/unit/test_task_spec.py -q"],
        suggested_risk_level="high",
        rationale="Needs manual gate while investigating CI impact.",
    )

    merged, report = apply_task_spec_brain_suggestion(
        task_spec=spec,
        suggestion=suggestion,
        provider="FakeBrain",
    )

    assert "CI runners are available." in merged.assumptions
    assert "Include root-cause summary in final output." in merged.acceptance_criteria
    assert "Do not merge changes automatically." in merged.non_goals
    assert merged.verification_commands == ["pytest tests/unit/test_task_spec.py -q"]
    assert merged.requires_clarification is True
    assert merged.risk_level == "high"
    assert merged.requires_permission is True
    assert "destructive_actions_without_permission" in merged.forbidden_actions
    assert report.provider == "FakeBrain"
    assert report.applied is True
    assert report.added_clarification_questions == ["Which CI job is failing most often?"]
    assert report.added_verification_commands == ["pytest tests/unit/test_task_spec.py -q"]
    assert report.ignored_fields == []
    assert validate_task_spec_policy(merged) == []


def test_apply_task_spec_brain_suggestion_clamps_unsafe_overrides() -> None:
    """Lower-risk and task-shape overrides are ignored to preserve deterministic boundaries."""
    spec = build_task_spec(
        task_text="Delete generated files in workspace",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
        constraints={"risk_level": "high"},
    )
    suggestion = TaskSpecBrainSuggestion(
        suggested_risk_level="low",
        suggested_task_type="docs",
        suggested_delivery_mode="summary",
        rationale="Try to reduce scope.",
    )

    merged, report = apply_task_spec_brain_suggestion(
        task_spec=spec,
        suggestion=suggestion,
        provider="FakeBrain",
    )

    assert merged.risk_level == spec.risk_level
    assert merged.task_type == spec.task_type
    assert merged.delivery_mode == spec.delivery_mode
    assert report.applied is False
    assert report.ignored_fields == [
        "suggested_task_type",
        "suggested_delivery_mode",
        "suggested_risk_level",
    ]
    assert validate_task_spec_policy(merged) == []


def test_build_task_spec_caps_and_dedupes_clarification_questions() -> None:
    """Clarification questions should be bounded to avoid overwhelming operators."""
    spec = build_task_spec(
        task_text="Investigate flaky tests",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
        constraints={
            "clarification_questions": [
                "Q1?",
                "Q2?",
                "Q1?",
                "Q3?",
                "Q4?",
            ]
        },
    )

    assert spec.clarification_questions == ["Q1?", "Q2?", "Q3?"]


def test_apply_task_spec_brain_suggestion_caps_clarification_questions() -> None:
    """Brain suggestions cannot grow clarification prompts beyond the cap."""
    spec = build_task_spec(
        task_text="Investigate flaky tests",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
        constraints={"clarification_questions": ["Base Q1?", "Base Q2?"]},
    )
    suggestion = TaskSpecBrainSuggestion(
        clarification_questions=["New Q3?", "Dropped Q4?"],
        rationale="Need additional context.",
    )

    merged, report = apply_task_spec_brain_suggestion(
        task_spec=spec,
        suggestion=suggestion,
        provider="FakeBrain",
    )

    assert merged.clarification_questions == ["Base Q1?", "Base Q2?", "New Q3?"]
    assert report.added_clarification_questions == ["New Q3?"]


@pytest.mark.asyncio
async def test_rule_based_orchestrator_brain_escalates_urgent_low_risk_task() -> None:
    """Urgent low-risk asks should trigger medium-risk escalation suggestion."""
    brain = RuleBasedOrchestratorBrain()
    task = TaskRequest(task_text="Urgent: fix this typo")
    deterministic_spec = build_task_spec(
        task_text=task.task_text,
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
    )
    assert deterministic_spec.risk_level == "low"

    suggestion = await brain.suggest_task_spec(
        task=task,
        task_kind="implementation",
        task_plan=None,
        task_spec=deterministic_spec,
    )

    assert suggestion is not None
    assert suggestion.suggested_risk_level == "medium"
