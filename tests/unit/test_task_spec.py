"""Unit tests for deterministic TaskSpec generation."""

from __future__ import annotations

import pytest

from orchestrator.brain import RuleBasedOrchestratorBrain, TaskSpecBrainSuggestion
from orchestrator.state import TaskRequest
from orchestrator.task_spec import (
    _max_risk,
    apply_task_spec_brain_suggestion,
    build_task_spec,
    build_task_spec_for_request,
    contains_marker,
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


def test_build_task_spec_marks_pwd_home_smoke_as_no_modification_summary() -> None:
    spec = build_task_spec(
        task_text="Smoke test: print PWD and HOME only, then exit.",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="master",
    )

    assert spec.task_type == "maintenance"
    assert spec.delivery_mode == "summary"
    assert "modify_workspace_files" not in spec.allowed_actions
    assert "Do not create or modify any files." in spec.non_goals
    assert spec.verification_commands == ['printf \'%s\\n%s\\n\' "$PWD" "$HOME"']
    assert spec.expected_artifacts == ["summary"]
    assert validate_task_spec_policy(spec) == []


def test_contains_marker_handles_empty_marker_sets_and_word_boundaries() -> None:
    """Marker detection should fail closed for empty sets and ignore partial-word matches."""
    assert contains_marker("Open a PR for this change", ()) is False
    assert contains_marker("surprising", ("pr",)) is False
    assert contains_marker("Open a PR for this change", ("pr",)) is True


def test_max_risk_defaults_to_low_for_unrecognized_inputs() -> None:
    """Unknown risk hints should fail closed to the safest default classification."""
    assert _max_risk("unexpected", "custom") == "low"


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


def test_build_task_spec_allows_specific_read_only_audit_without_clarification() -> None:
    """The fan-out QA fixture must enter the read-only DAG instead of waiting for input."""
    spec = build_task_spec(
        task_text=(
            "Audit two independent read-only repository inspections across files: summarize "
            "README.md and list the top-level tracked files. "
            "Do not modify files, create artifacts, or commit."
        ),
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="master",
        constraints={"read_only": True},
        task_kind="ambiguous",
    )

    assert spec.requires_clarification is False
    assert spec.clarification_questions == []
    assert spec.delivery_mode == "summary"
    assert validate_task_spec_policy(spec) == []


@pytest.mark.parametrize(
    ("task_text", "expected_type", "expected_risk"),
    [
        ("Update README docs for local setup", "docs", "low"),
        ("Address requested changes from the PR", "review_fix", "medium"),
        ("Refresh CI dependency pins", "maintenance", "low"),
    ],
)
def test_build_task_spec_classifies_marker_driven_task_types(
    task_text: str,
    expected_type: str,
    expected_risk: str,
) -> None:
    """Classification markers should map critical task shapes into the right TaskSpec buckets."""
    spec = build_task_spec(
        task_text=task_text,
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
    )

    assert spec.task_type == expected_type
    assert spec.risk_level == expected_risk
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


def test_build_task_spec_treats_explicit_destructive_constraint_as_high_risk() -> None:
    """Operator-supplied destructive hints should trigger the same approval guard as markers."""
    spec = build_task_spec(
        task_text="Refresh generated output",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
        constraints={"destructive_action": True},
    )

    assert spec.risk_level == "high"
    assert spec.requires_permission is True
    assert spec.permission_reason == "Task is classified as high risk."


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


@pytest.mark.parametrize(
    ("task_text", "expected_delivery_mode"),
    [
        ("Prepare a branch for the fix", "branch"),
        ("Open a draft PR once the fix is ready", "draft_pr"),
        ("Investigate the failure and send a summary only", "summary"),
    ],
)
def test_build_task_spec_detects_delivery_mode_markers(
    task_text: str,
    expected_delivery_mode: str,
) -> None:
    """Delivery markers should change expected artifacts and operator handoff semantics."""
    spec = build_task_spec(
        task_text=task_text,
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
        constraints={"delivery_mode": "unsupported-mode"},
    )

    assert spec.delivery_mode == expected_delivery_mode


def test_build_task_spec_escalates_critical_risk_requests() -> None:
    """Secret/auth/deploy asks should be gated as critical-risk work."""
    spec = build_task_spec(
        task_text="Rotate secrets before the production deploy",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
    )

    assert spec.risk_level == "critical"
    assert spec.requires_permission is True
    assert spec.permission_reason == "Task is classified as critical risk."
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


def test_build_task_spec_for_request_preserves_normalized_request_fields() -> None:
    """TaskRequest wrappers should produce the same persisted TaskSpec contract as direct calls."""
    request = TaskRequest(
        task_text="Open a draft PR for the README docs",
        repo_url="https://github.com/natanayalo/code-agent",
        branch="docs/update-readme",
        constraints={"verification_commands": ["pytest tests/unit/test_task_spec.py -q"]},
    )

    spec = build_task_spec_for_request(request, task_kind=None, task_plan=None)

    assert spec.goal == "Open a draft PR for the README docs"
    assert spec.repo_url == request.repo_url
    assert spec.target_branch == request.branch
    assert spec.task_type == "docs"
    assert spec.delivery_mode == "draft_pr"
    assert spec.verification_commands == ["pytest tests/unit/test_task_spec.py -q"]


@pytest.mark.parametrize(
    ("overrides", "expected_violation"),
    [
        (
            {"requires_clarification": True, "clarification_questions": []},
            "clarification_required_without_questions",
        ),
        (
            {"risk_level": "critical", "requires_permission": False},
            "high_risk_without_permission_gate",
        ),
        (
            {"requires_permission": True, "permission_reason": None},
            "permission_required_without_reason",
        ),
        (
            {"forbidden_actions": []},
            "missing_secret_hardcode_forbidden_action",
        ),
    ],
)
def test_validate_task_spec_policy_reports_missing_safety_guards(
    overrides: dict[str, object],
    expected_violation: str,
) -> None:
    """Policy validation should flag each missing safety guard independently."""
    baseline = build_task_spec(
        task_text="Implement the requested change",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
    )

    candidate = baseline.model_copy(update=overrides)

    assert validate_task_spec_policy(candidate) == [expected_violation]


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


def test_build_task_spec_with_delivery_fields() -> None:
    spec = build_task_spec(
        task_text="Fix issue and open a draft pr",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="master",
        constraints={
            "delivery_branch": "fix/issue",
            "pr_title": "Fix issue",
            "pr_body": "This fixes the issue.",
        },
    )

    assert spec.delivery_mode == "draft_pr"
    assert spec.delivery_branch == "fix/issue"
    assert spec.pr_title == "Fix issue"
    assert spec.pr_body == "This fixes the issue."


def test_brain_suggestion_adds_pr_fields() -> None:
    spec = build_task_spec(
        task_text="Add feature and open pr",
        repo_url="repo",
        target_branch="main",
    )
    suggestion = TaskSpecBrainSuggestion(
        suggested_delivery_branch="feat/new",
        suggested_pr_title="New Feature",
    )
    merged, report = apply_task_spec_brain_suggestion(task_spec=spec, suggestion=suggestion)

    assert merged.delivery_branch == "feat/new"
    assert merged.pr_title == "New Feature"
    assert "suggested_delivery_branch" not in report.ignored_fields


def test_task_spec_strips_delivery_branch_whitespace() -> None:
    from orchestrator.state import TaskSpec

    spec = TaskSpec(goal="Test goal", delivery_branch="  main  ")
    assert spec.delivery_branch == "main"


def test_build_task_spec_forces_summary_delivery_for_scout_tasks() -> None:
    """Scout tasks must use summary delivery even if draft_pr is requested."""
    spec = build_task_spec(
        task_text="Scout the codebase",
        repo_url="https://github.com/natanayalo/code-agent",
        target_branch="main",
        constraints={
            "task_type": "scout",
            "delivery_mode": "draft_pr",
        },
    )

    assert spec.task_type == "scout"
    assert spec.delivery_mode == "summary"
    assert "prepare_draft_pr_delivery" not in spec.allowed_actions
    assert "workspace_diff" not in spec.expected_artifacts
    assert validate_task_spec_policy(spec) == []
