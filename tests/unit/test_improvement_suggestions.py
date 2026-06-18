"""Unit tests for deterministic improvement suggestion synthesis."""

from orchestrator.improvement_suggestions import (
    build_improvement_suggestion_draft,
    compute_friction_fingerprint,
)
from orchestrator.reflection import FrictionReport


def test_sandbox_blocked_friction_scores_high_risk_and_hitl_required() -> None:
    report = FrictionReport(
        task_id="task-1",
        worker_run_id="run-1",
        source="sandbox",
        description="Infra crash prevented checkout.",
        impact="blocked",
        context={"failure_kind": "sandbox_infra"},
    )

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-1",
        attempt_count=2,
        retry_context=True,
    )

    assert draft.suggestion.title == "Harden sandbox infrastructure recovery"
    assert draft.suggestion.value == "high"
    assert draft.suggestion.effort == "large"
    assert draft.suggestion.risk == "high"
    assert draft.suggestion.layer_impact == "sandbox"
    assert draft.suggestion.hitl_need == "required"
    assert "sandbox runner integration tests" in draft.suggestion.validation_path


def test_instruction_friction_scores_lower_risk_worker_layer() -> None:
    report = FrictionReport(
        source="instructions",
        description="Instructions forced an unnecessary workaround.",
        impact="required_workaround",
    )

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-2",
        attempt_count=1,
    )

    assert draft.suggestion.value == "medium"
    assert draft.suggestion.effort == "small"
    assert draft.suggestion.risk == "low"
    assert draft.suggestion.layer_impact == "worker"
    assert draft.suggestion.hitl_need == "none"
    assert "worker/orchestrator tests" in draft.suggestion.validation_path


def test_missing_description_still_produces_valid_suggestion_text() -> None:
    report = FrictionReport(source="other", description=None, impact="unknown")

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-3",
        attempt_count=0,
    )

    assert draft.suggestion.title == "Improve other friction handling"
    assert draft.suggestion.description == (
        "Reduce recurring other friction observed during task execution."
    )
    assert draft.suggestion.validation_path


def test_whitespace_description_uses_source_title_fallback() -> None:
    report = FrictionReport(source="other", description="   ", impact="unknown")

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-whitespace",
        attempt_count=0,
    )

    assert draft.suggestion.title == "Improve other friction handling"
    assert draft.suggestion.description == (
        "Reduce recurring other friction observed during task execution."
    )


def test_short_split_title_preserves_context_without_fallback() -> None:
    report = FrictionReport(source="tooling", description="A: B", impact="slowed_down")

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-short-title",
        attempt_count=0,
    )

    assert draft.suggestion.title == "Improve A: B handling"


def test_title_generation_preserves_urls_and_file_line_references() -> None:
    url_report = FrictionReport(
        source="tooling",
        description="http://localhost:8000 returned 500",
        impact="slowed_down",
    )
    file_report = FrictionReport(
        source="tooling",
        description="main.py:42 raised ValueError",
        impact="slowed_down",
    )

    url_draft = build_improvement_suggestion_draft(
        url_report,
        task_id="task-url-title",
        attempt_count=0,
    )
    file_draft = build_improvement_suggestion_draft(
        file_report,
        task_id="task-file-title",
        attempt_count=0,
    )

    assert url_draft.suggestion.title == "Improve http://localhost:8000 returned 500 handling"
    assert file_draft.suggestion.title == "Improve main.py:42 raised ValueError handling"


def test_title_generation_strips_trailing_punctuation() -> None:
    period_report = FrictionReport(
        source="tooling",
        description="Dependency install failed.",
        impact="slowed_down",
    )
    colon_report = FrictionReport(
        source="tooling",
        description="Failed to execute:",
        impact="slowed_down",
    )

    period_draft = build_improvement_suggestion_draft(
        period_report,
        task_id="task-period-title",
        attempt_count=0,
    )
    colon_draft = build_improvement_suggestion_draft(
        colon_report,
        task_id="task-colon-title",
        attempt_count=0,
    )

    assert period_draft.suggestion.title == "Improve Dependency install failed handling"
    assert colon_draft.suggestion.title == "Improve Failed to execute handling"


def test_title_generation_uses_specific_suffix_after_generic_prefix() -> None:
    report = FrictionReport(
        source="tooling",
        description="Error: pip install failed",
        impact="slowed_down",
    )

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-generic-prefix-title",
        attempt_count=0,
    )

    assert draft.suggestion.title == "Improve pip install failed handling"


def test_title_generation_truncates_after_generic_prefix() -> None:
    suffix = (
        "dependency resolver kept backtracking on pinned versions after cache refresh "
        "while installing constraints"
    )
    report = FrictionReport(
        source="tooling",
        description=f"Failed to execute command: {suffix}",
        impact="slowed_down",
    )

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-long-generic-prefix-title",
        attempt_count=0,
    )

    assert draft.suggestion.title == f"Improve {suffix[:80]} handling"


def test_fingerprint_is_stable_and_changes_with_identity_fields() -> None:
    report = FrictionReport(
        source="tooling",
        description="Repeated test failure.",
        impact="blocked",
    )
    same_report = FrictionReport(
        source="tooling",
        description="Repeated test failure.",
        impact="blocked",
    )
    changed_report = FrictionReport(
        source="tooling",
        description="Different failure.",
        impact="blocked",
    )

    fingerprint = compute_friction_fingerprint(report, task_id="task-4")

    assert len(fingerprint) == 64
    assert fingerprint == compute_friction_fingerprint(same_report, task_id="task-4")
    assert fingerprint != compute_friction_fingerprint(changed_report, task_id="task-4")


def test_fingerprint_normalizes_nullable_source_and_impact_defaults() -> None:
    defaulted_report = FrictionReport(
        source="other",
        description="Same friction.",
        impact="unknown",
    )
    nullable_report = FrictionReport(
        source=None,
        description="Same friction.",
        impact=None,
    )

    assert compute_friction_fingerprint(defaulted_report, task_id="task-5") == (
        compute_friction_fingerprint(nullable_report, task_id="task-5")
    )


def test_fingerprint_strips_description_whitespace() -> None:
    stripped_report = FrictionReport(
        source="tooling",
        description="Same friction.",
        impact="unknown",
    )
    padded_report = FrictionReport(
        source="tooling",
        description="  Same friction.  ",
        impact="unknown",
    )
    empty_report = FrictionReport(source="tooling", description=None, impact="unknown")
    whitespace_report = FrictionReport(source="tooling", description="   ", impact="unknown")

    assert compute_friction_fingerprint(stripped_report, task_id="task-6") == (
        compute_friction_fingerprint(padded_report, task_id="task-6")
    )
    assert compute_friction_fingerprint(empty_report, task_id="task-6") == (
        compute_friction_fingerprint(whitespace_report, task_id="task-6")
    )
