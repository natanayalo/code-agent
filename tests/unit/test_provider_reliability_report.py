"""Unit test coverage for M29 provider reliability contracts, math, and rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from evaluation.provider_reliability_extractor import (
    compute_wilson_interval,
    determine_task_mutation_mode,
    generate_recommendations,
    is_profile_compatible_with_mode,
)
from evaluation.provider_reliability_models import (
    BudgetCoverageMetrics,
    InterventionMetrics,
    LatencyMetrics,
    ProviderReliabilityEvidenceCell,
    ProviderReliabilityReport,
    ReliabilityReportPolicy,
    RepairMetrics,
    StageOutcomeRates,
    TaskExclusionSummary,
    WilsonConfidenceInterval,
)
from evaluation.provider_reliability_report import (
    assert_sanitized_report,
    render_json_report,
    render_markdown_report,
)


def test_wilson_interval_bounds_and_zero_samples() -> None:
    """Test Wilson interval boundaries, zero sample handling, and precision."""
    zero_ci = compute_wilson_interval(0, 0)
    assert zero_ci == WilsonConfidenceInterval(lower=0.0, center=0.0, upper=0.0)

    perfect_ci = compute_wilson_interval(10, 10, confidence_level=0.95)
    assert 0.70 <= perfect_ci.lower <= 0.75
    assert perfect_ci.upper == 1.0

    zero_success_ci = compute_wilson_interval(0, 10, confidence_level=0.95)
    assert zero_success_ci.lower == 0.0
    assert 0.25 <= zero_success_ci.upper <= 0.35


def test_sample_gating_nine_versus_ten() -> None:
    """Validate 9-versus-10 task sample eligibility gating."""
    policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=datetime(2026, 9, 16, tzinfo=UTC),
        window_start_at=datetime(2026, 6, 18, tzinfo=UTC),
        window_end_at=datetime(2026, 9, 16, tzinfo=UTC),
    )

    cell_9 = ProviderReliabilityEvidenceCell(
        task_class="bugfix",
        profile="codex-native-executor",
        mutation_mode="mutation",
        sample_size=9,
        accepted_count=9,
        accepted_task_rate=1.0,
        accepted_task_rate_ci=compute_wilson_interval(9, 9),
        failure_count=0,
        failure_rate=0.0,
        stage_outcome_rates=StageOutcomeRates(dispatch_rate=1.0, execution_success_rate=1.0),
        repairs=RepairMetrics(
            verifier_repairs_count=0,
            review_repairs_count=0,
            total_repaired_tasks=0,
            repair_rate=0.0,
        ),
        interventions=InterventionMetrics(
            human_interventions_count=0,
            clarification_questions_count=0,
            approvals_count=0,
            intervention_rate=0.0,
        ),
        manual_overrides_count=0,
        manual_override_rate=0.0,
        terminal_latency=LatencyMetrics(median_seconds=45.0),
        budget_field_coverage=BudgetCoverageMetrics(
            budget_reported_count=9, budget_coverage_rate=1.0
        ),
        is_eligible=False,
        insufficiency_reasons=[f"insufficient_sample_size: 9 tasks (minimum {policy.min_samples})"],
    )
    assert not cell_9.is_eligible
    assert "insufficient_sample_size" in cell_9.insufficiency_reasons[0]

    cell_10 = cell_9.model_copy(
        update={
            "sample_size": 10,
            "is_eligible": True,
            "insufficiency_reasons": [],
            "accepted_count": 10,
            "accepted_task_rate_ci": compute_wilson_interval(10, 10),
        }
    )
    assert cell_10.is_eligible
    assert not cell_10.insufficiency_reasons


def test_profile_mode_compatibility() -> None:
    """Test read-only versus mutation profile compatibility matching."""
    assert is_profile_compatible_with_mode("codex-native-executor-read-only", "read_only")
    assert not is_profile_compatible_with_mode("codex-native-executor-read-only", "mutation")
    assert is_profile_compatible_with_mode("codex-native-executor", "mutation")
    assert not is_profile_compatible_with_mode("codex-native-executor", "read_only")
    assert is_profile_compatible_with_mode("antigravity-native-executor-read-only", "read_only")
    assert is_profile_compatible_with_mode("antigravity-native-executor", "mutation")


def test_determine_task_mutation_mode() -> None:
    """Test resolution of task mutation mode from task specs, constraints, and profiles."""
    scout_spec = {"task_type": "scout", "allowed_actions": []}
    assert determine_task_mutation_mode(scout_spec, None, None) == "read_only"

    ro_spec = {"task_type": "feature", "allowed_actions": ["read_files"]}
    assert determine_task_mutation_mode(ro_spec, None, None) == "read_only"

    mut_spec = {"task_type": "feature", "allowed_actions": ["modify_workspace_files"]}
    assert determine_task_mutation_mode(mut_spec, None, None) == "mutation"

    assert determine_task_mutation_mode(None, {"read_only": True}, None) == "read_only"
    assert (
        determine_task_mutation_mode(None, None, "antigravity-native-executor-read-only")
        == "read_only"
    )
    assert determine_task_mutation_mode(None, {}, "codex-native-executor") == "mutation"


def _make_dummy_cell(
    profile: str,
    wilson_lower: float,
    median_latency: float | None,
    is_eligible: bool = True,
) -> ProviderReliabilityEvidenceCell:
    """Helper to construct dummy evidence cell for recommendation testing."""
    return ProviderReliabilityEvidenceCell(
        task_class="feature",
        profile=profile,
        mutation_mode="mutation",
        sample_size=10 if is_eligible else 5,
        accepted_count=10 if is_eligible else 5,
        accepted_task_rate=1.0,
        accepted_task_rate_ci=WilsonConfidenceInterval(
            lower=wilson_lower, center=wilson_lower + 0.1, upper=1.0
        ),
        failure_count=0,
        failure_rate=0.0,
        stage_outcome_rates=StageOutcomeRates(dispatch_rate=1.0, execution_success_rate=1.0),
        repairs=RepairMetrics(
            verifier_repairs_count=0,
            review_repairs_count=0,
            total_repaired_tasks=0,
            repair_rate=0.0,
        ),
        interventions=InterventionMetrics(
            human_interventions_count=0,
            clarification_questions_count=0,
            approvals_count=0,
            intervention_rate=0.0,
        ),
        manual_overrides_count=0,
        manual_override_rate=0.0,
        terminal_latency=LatencyMetrics(median_seconds=median_latency),
        budget_field_coverage=BudgetCoverageMetrics(
            budget_reported_count=10, budget_coverage_rate=1.0
        ),
        is_eligible=is_eligible,
        insufficiency_reasons=[] if is_eligible else ["insufficient_sample_size: 5 tasks"],
    )


def test_recommendation_ranking_and_deterministic_ties() -> None:
    """Test ranking by Wilson lower bound, latency, and alphabetical profile tie-breaking."""
    c1 = _make_dummy_cell("codex-native-executor", wilson_lower=0.75, median_latency=60.0)
    c2 = _make_dummy_cell("antigravity-native-executor", wilson_lower=0.85, median_latency=50.0)
    recs = generate_recommendations([c1, c2])
    assert len(recs) == 1
    assert recs[0].recommended_profile == "antigravity-native-executor"
    assert recs[0].fallback_reason is None
    assert recs[0].rankings[0].profile == "antigravity-native-executor"
    assert recs[0].rankings[0].rank == 1
    assert recs[0].rankings[1].profile == "codex-native-executor"
    assert recs[0].rankings[1].rank == 2

    # Equal Wilson lower, tie-break by latency
    c1_lat = _make_dummy_cell("codex-native-executor", wilson_lower=0.80, median_latency=40.0)
    c2_lat = _make_dummy_cell("antigravity-native-executor", wilson_lower=0.80, median_latency=80.0)
    recs_lat = generate_recommendations([c1_lat, c2_lat])
    assert recs_lat[0].recommended_profile == "codex-native-executor"

    # Equal Wilson lower and latency, tie-break by alphabetical profile name
    c1_alpha = _make_dummy_cell("beta-native-executor", wilson_lower=0.80, median_latency=50.0)
    c2_alpha = _make_dummy_cell("alpha-native-executor", wilson_lower=0.80, median_latency=50.0)
    recs_alpha = generate_recommendations([c1_alpha, c2_alpha])
    assert recs_alpha[0].recommended_profile == "alpha-native-executor"


def test_recommendation_fallback_when_insufficient_candidates() -> None:
    """Test recommendation fallbacks when < 2 candidates are eligible."""
    c_single = _make_dummy_cell("codex-native-executor", wilson_lower=0.85, median_latency=50.0)
    recs_1 = generate_recommendations([c_single])
    assert recs_1[0].recommended_profile is None
    assert "insufficient_eligible_candidates: only 1 eligible candidate" in str(
        recs_1[0].fallback_reason
    )

    c_ineligible = _make_dummy_cell(
        "antigravity-native-executor",
        wilson_lower=0.50,
        median_latency=60.0,
        is_eligible=False,
    )
    recs_mixed = generate_recommendations([c_single, c_ineligible])
    assert recs_mixed[0].recommended_profile is None
    assert "only 1 eligible candidate" in str(recs_mixed[0].fallback_reason)

    c_none = _make_dummy_cell(
        "codex-native-executor", wilson_lower=0.5, median_latency=50.0, is_eligible=False
    )
    recs_0 = generate_recommendations([c_none])
    assert recs_0[0].recommended_profile is None
    assert "no_eligible_candidates" in str(recs_0[0].fallback_reason)


def test_sanitization_guardrail_rejects_private_fields() -> None:
    """Ensure assert_sanitized_report strictly forbids private fields."""
    clean_payload = {
        "status": "complete",
        "evidence_cells": [{"task_class": "feature", "profile": "codex-native-executor"}],
    }
    assert_sanitized_report(clean_payload)
    assert_sanitized_report(json.dumps(clean_payload))

    for forbidden in ["task_id", "task_text", "repo_url", "branch", "secret", "secrets", "summary"]:
        dirty_dict = {
            "status": "complete",
            "evidence_cells": [{forbidden: "private-value"}],
        }
        with pytest.raises(ValueError, match="Forbidden key detected"):
            assert_sanitized_report(dirty_dict)

        with pytest.raises(ValueError, match="Forbidden key detected"):
            assert_sanitized_report(json.dumps(dirty_dict))


def test_report_rendering_json_and_markdown() -> None:
    """Test deterministic JSON and Markdown output generation."""
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=now,
        window_start_at=now - timedelta(days=90),
        window_end_at=now,
    )
    cell = _make_dummy_cell("codex-native-executor", wilson_lower=0.85, median_latency=35.5)
    rec = generate_recommendations([cell])[0]

    report = ProviderReliabilityReport(
        schema_version=1,
        generated_at=now,
        status="insufficient_data",
        policy=policy,
        exclusions=TaskExclusionSummary(
            total_tasks_scanned=20,
            included_tasks_count=10,
            excluded_tasks_count=10,
            by_reason={"cancelled": 5, "malformed_missing_profile": 5},
        ),
        evidence_cells=[cell],
        recommendations=[rec],
    )

    json_str = render_json_report(report)
    loaded = json.loads(json_str)
    assert loaded["schema_version"] == 1
    assert loaded["status"] == "insufficient_data"
    assert loaded["exclusions"]["total_tasks_scanned"] == 20

    md_str = render_markdown_report(report)
    assert "# M29 Provider Reliability Advisory Report" in md_str
    assert "Evidence Accounting & Exclusions" in md_str
    assert "Recommendations by Task Class and Mode" in md_str
    assert "Evidence Cells Summary" in md_str


def test_assert_sanitized_report_invalid_json_and_list_keys() -> None:
    """Test assert_sanitized_report on invalid JSON strings and nested list elements."""
    with pytest.raises(ValueError, match="invalid report JSON payload"):
        assert_sanitized_report("not-valid-json {")

    nested_list_payload = {"items": [{"task_text": "private text"}]}
    with pytest.raises(ValueError, match="Forbidden key detected"):
        assert_sanitized_report(nested_list_payload)


def test_render_markdown_empty_tables() -> None:
    """Test markdown rendering when recommendations and evidence cells are empty."""
    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=now,
        window_start_at=now - timedelta(days=90),
        window_end_at=now,
    )
    report = ProviderReliabilityReport(
        schema_version=1,
        generated_at=now,
        status="insufficient_data",
        policy=policy,
        exclusions=TaskExclusionSummary(
            total_tasks_scanned=0, included_tasks_count=0, excluded_tasks_count=0
        ),
        evidence_cells=[],
        recommendations=[],
    )
    md_str = render_markdown_report(report)
    assert "_No recommendations generated._" in md_str
    assert "_No evidence cells observed._" in md_str
