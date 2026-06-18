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
