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
    assert draft.suggestion.description
    assert "other friction was reported" in draft.suggestion.description
    assert draft.suggestion.validation_path


def test_whitespace_description_uses_source_title_fallback() -> None:
    report = FrictionReport(source="other", description="   ", impact="unknown")

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-whitespace",
        attempt_count=0,
    )

    assert draft.suggestion.title == "Improve other friction handling"
    assert "reported without a detailed description" in draft.suggestion.description


def test_short_split_title_uses_source_title_fallback() -> None:
    report = FrictionReport(source="tooling", description="A: B", impact="slowed_down")

    draft = build_improvement_suggestion_draft(
        report,
        task_id="task-short-title",
        attempt_count=0,
    )

    assert draft.suggestion.title == "Improve tooling friction handling"


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
