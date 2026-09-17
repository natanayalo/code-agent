"""Unit test coverage for M29 provider reliability contracts, math, and rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from evaluation.provider_reliability_extractor import (
    compute_wilson_interval,
    determine_report_status,
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
    TaskClassRecommendation,
    TaskExclusionSummary,
    WilsonConfidenceInterval,
)
from evaluation.provider_reliability_report import (
    assert_sanitized_report,
    escape_markdown,
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
    sample_size: int | None = None,
    accepted: int | None = None,
) -> ProviderReliabilityEvidenceCell:
    """Helper to construct dummy evidence cell for recommendation testing."""
    s_size = sample_size if sample_size is not None else (10 if is_eligible else 5)
    a_count = accepted if accepted is not None else (10 if is_eligible else 5)
    return ProviderReliabilityEvidenceCell(
        task_class="feature",
        profile=profile,
        mutation_mode="mutation",
        sample_size=s_size,
        accepted_count=a_count,
        accepted_task_rate=round(a_count / s_size, 4) if s_size else 0.0,
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


def test_sanitizer_rejects_unsafe_values_and_unknown_domains() -> None:
    """Ensure assert_sanitized_report rejects unsafe substrings, URLs, and paths."""
    valid_payload = {
        "status": "complete",
        "evidence_cells": [
            {
                "task_class": "feature",
                "profile": "codex-native-executor",
                "mutation_mode": "mutation",
                "sample_size": 10,
                "accepted_count": 10,
                "accepted_task_rate": 1.0,
                "failure_count": 0,
                "failure_rate": 0.0,
                "manual_overrides_count": 0,
                "manual_override_rate": 0.0,
                "is_eligible": True,
                "insufficiency_reasons": [],
            }
        ],
    }
    assert_sanitized_report(valid_payload)

    for unsafe in [
        "https://github.com/org/repo.git",
        "http://internal.service/api",
        "/Users/alice/dev/project",
        "/home/ubuntu/repo",
        "/tmp/scratch.txt",
    ]:
        bad_payload = {
            "status": "complete",
            "evidence_cells": [
                {
                    "task_class": "feature",
                    "profile": "codex-native-executor",
                    "insufficiency_reasons": [unsafe],
                }
            ],
        }
        with pytest.raises(ValueError, match="Unsafe substring"):
            assert_sanitized_report(bad_payload)

    for bad_profile in ["codex;rm -rf /", "profile with spaces", "INVALID_UPPERCASE"]:
        bad_profile_payload = {
            "status": "complete",
            "evidence_cells": [{"profile": bad_profile}],
        }
        with pytest.raises(ValueError, match="Invalid profile identifier format"):
            assert_sanitized_report(bad_profile_payload)


def test_markdown_escaping() -> None:
    """Ensure escape_markdown escapes pipes and cleans newlines."""
    assert escape_markdown("a|b|c") == r"a\|b\|c"
    assert escape_markdown("line1\nline2") == "line1 line2"
    assert escape_markdown(None) == "N/A"
    assert escape_markdown(123) == "123"


def test_candidate_ranking_recency_and_counts() -> None:
    """Verify CandidateRanking includes recency, sample size, and accepted counts."""
    ref_as_of = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    cell = _make_dummy_cell(
        "codex-native-executor",
        sample_size=15,
        accepted=12,
        wilson_lower=0.75,
        median_latency=45.0,
    )
    object.__setattr__(cell, "newest_evidence_timestamp", ref_as_of - timedelta(days=2))
    object.__setattr__(cell, "oldest_evidence_timestamp", ref_as_of - timedelta(days=30))

    recs = generate_recommendations([cell], as_of=ref_as_of)
    assert len(recs) == 1
    ranking = recs[0].rankings[0]

    assert ranking.sample_size == 15
    assert ranking.accepted_count == 12
    assert ranking.evidence_age_days == 2.0
    assert ranking.newest_evidence_timestamp == ref_as_of - timedelta(days=2)
    assert ranking.oldest_evidence_timestamp == ref_as_of - timedelta(days=30)


def test_determine_report_status_states() -> None:
    """Verify report status logic across complete, partial, and insufficient_data."""
    assert determine_report_status([]) == "insufficient_data"

    rec_completed = TaskClassRecommendation(
        task_class="feature",
        mutation_mode="mutation",
        recommended_profile="codex-native-executor",
    )
    rec_insufficient = TaskClassRecommendation(
        task_class="scout",
        mutation_mode="read_only",
        recommended_profile=None,
        fallback_reason="no_eligible_candidates",
    )

    assert determine_report_status([rec_completed]) == "complete"
    assert determine_report_status([rec_insufficient]) == "insufficient_data"
    assert determine_report_status([rec_completed, rec_insufficient]) == "partial"


def test_provider_reliability_stages_direct() -> None:
    """Direct unit tests for stage checking, artifact extraction, and failure kinds."""
    from db.enums import ArtifactType, TaskStatus, WorkerRunStatus
    from db.models import Task, WorkerRun
    from evaluation.provider_reliability_stages import (
        check_delivery_stage,
        check_review_stage,
        check_verification_stage,
        resolve_failure_kind,
        verify_timeline_consistency,
    )

    consistent, ts = verify_timeline_consistency(TaskStatus.IN_PROGRESS, [], None)
    assert not consistent and ts is None

    consistent, ts = verify_timeline_consistency(TaskStatus.FAILED, [], None)
    assert not consistent and ts is None

    task = Task(status=TaskStatus.COMPLETED, task_spec={"verification_commands": ["test"]})
    run_passed = WorkerRun(status=WorkerRunStatus.SUCCESS, verifier_outcome={"status": "passed"})
    run_failed = WorkerRun(status=WorkerRunStatus.FAILURE, verifier_outcome={"status": "failed"})
    assert check_verification_stage(task, [run_passed]) == (True, True)
    assert check_verification_stage(task, [run_failed]) == (True, False)

    run_with_review_pass = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        artifact_index=[
            {
                "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
                "artifact_metadata": {
                    ArtifactType.INDEPENDENT_REVIEW_RESULT.value: {
                        "outcome": "no_findings",
                        "findings": [],
                    }
                },
            }
        ],
    )
    assert check_review_stage(task, [run_with_review_pass], v_passed=True) == (True, True)

    run_with_review_fail = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        artifact_index=[
            {
                "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
                "artifact_metadata": {
                    ArtifactType.INDEPENDENT_REVIEW_RESULT.value: {
                        "outcome": "findings",
                        "findings": [{"message": "bug"}],
                    }
                },
            }
        ],
    )
    assert check_review_stage(task, [run_with_review_fail], v_passed=True) == (True, False)

    task_repair = Task(
        status=TaskStatus.COMPLETED,
        task_spec={"allowed_actions": ["modify_workspace_files"]},
        constraints={"independent_review_repair_passes_used": 1},
    )
    assert check_review_stage(task_repair, [], v_passed=True) == (False, False)

    run_delivery = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        delivery_metadata={"branch": "refs/heads/feature"},
    )
    task_branch = Task(status=TaskStatus.COMPLETED, task_spec={"delivery_mode": "branch"})
    assert check_delivery_stage(task_branch, [run_delivery]) == (True, False)

    task_err = Task(status=TaskStatus.FAILED, last_error="boom")
    assert resolve_failure_kind(task_err, accepted=False, runs=[]) == "task_error"
    task_unknown = Task(status=TaskStatus.FAILED, last_error=None)
    assert resolve_failure_kind(task_unknown, accepted=False, runs=[]) == "unknown"


def test_sanitizer_rejects_invalid_domains() -> None:
    """Ensure assert_sanitized_report strictly validates against finite allowed domains."""
    # Invalid task_class
    with pytest.raises(ValueError, match="Invalid task_class domain value"):
        assert_sanitized_report({"evidence_cells": [{"task_class": "arbitrary_unknown_class"}]})

    # Invalid mutation_mode
    with pytest.raises(ValueError, match="Invalid mutation_mode domain value"):
        assert_sanitized_report({"evidence_cells": [{"mutation_mode": "arbitrary_unknown_mode"}]})

    # Invalid typed_failures key
    with pytest.raises(ValueError, match="Invalid typed failure key"):
        assert_sanitized_report(
            {"evidence_cells": [{"typed_failures": {"unauthorized_failure_kind": 1}}]}
        )

    # Valid typed_failures key with canonical worker failure "compile"
    assert_sanitized_report({"evidence_cells": [{"typed_failures": {"compile": 1}}]})

    # Invalid exclusions.by_reason key
    with pytest.raises(ValueError, match="Invalid exclusion reason key"):
        assert_sanitized_report({"exclusions": {"by_reason": {"unauthorized_exclusion_reason": 1}}})


def test_provider_reliability_verification_and_review_branches() -> None:
    """Test verification and independent review edge branches."""
    from db.enums import ArtifactType, TaskStatus, TimelineEventType, WorkerRunStatus
    from db.models import Task, TaskTimelineEvent, WorkerRun
    from evaluation.provider_reliability_stages import (
        check_review_stage,
        check_verification_stage,
    )

    # Verification stage with failed timeline event
    task_v_fail = Task(
        status=TaskStatus.FAILED,
        task_spec={"verification_commands": ["test"]},
        timeline_events=[
            TaskTimelineEvent(
                attempt_number=0,
                sequence_number=0,
                event_type=TimelineEventType.VERIFICATION_COMPLETED,
                payload={"status": "failed"},
            )
        ],
    )
    assert check_verification_stage(task_v_fail, []) == (True, False)

    # Verification stage with empty run verifier_outcome fallback
    task_v_empty = Task(status=TaskStatus.COMPLETED, task_spec={"verification_commands": ["test"]})
    run_empty = WorkerRun(status=WorkerRunStatus.SUCCESS, verifier_outcome={})
    assert check_verification_stage(task_v_empty, [run_empty]) == (True, False)

    # Worker self-review (REVIEW_RESULT) does NOT make independent review applicable
    run_self_review = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        artifact_index=[
            {
                "artifact_type": ArtifactType.REVIEW_RESULT.value,
                "artifact_metadata": {
                    ArtifactType.REVIEW_RESULT.value: {
                        "outcome": "no_findings",
                        "findings": [],
                    }
                },
            }
        ],
    )
    assert check_review_stage(task_v_empty, [run_self_review], v_passed=True) == (False, False)

    # Independent review stage with custom dict metadata key and approved: True
    run_approved = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        artifact_index=[
            {
                "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
                "artifact_metadata": {"custom_result": {"approved": True}},
            }
        ],
    )
    assert check_review_stage(task_v_empty, [run_approved], v_passed=True) == (True, True)

    # Independent review stage with custom dict metadata key and rejected status
    run_rejected = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        artifact_index=[
            {
                "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
                "artifact_metadata": {"custom_result": {"status": "rejected"}},
            }
        ],
    )
    assert check_review_stage(task_v_empty, [run_rejected], v_passed=True) == (True, False)


def test_provider_reliability_delivery_branches() -> None:
    """Test delivery stage: metadata without broker event fails, event succeeds or fails."""
    from db.enums import TaskStatus, TimelineEventType, WorkerRunStatus
    from db.models import Task, TaskTimelineEvent, WorkerRun
    from evaluation.provider_reliability_stages import check_delivery_stage

    # Delivery stage with metadata only (no DELIVERY_COMPLETED event) returns False
    run_deliv_meta = WorkerRun(
        status=WorkerRunStatus.SUCCESS,
        delivery_metadata={"branch": "refs/heads/feature", "pr_url": "https://example.com/pr/1"},
    )
    task_deliv = Task(status=TaskStatus.COMPLETED, task_spec={"delivery_mode": "branch"})
    assert check_delivery_stage(task_deliv, [run_deliv_meta]) == (True, False)

    # Delivery stage with DELIVERY_COMPLETED timeline event returns True
    task_deliv.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=0,
            event_type=TimelineEventType.DELIVERY_COMPLETED,
        )
    )
    assert check_delivery_stage(task_deliv, [run_deliv_meta]) == (True, True)

    # Delivery stage with DELIVERY_FAILED timeline event returns False
    task_d_fail = Task(
        status=TaskStatus.FAILED,
        task_spec={"delivery_mode": "branch"},
        timeline_events=[
            TaskTimelineEvent(
                attempt_number=0,
                sequence_number=0,
                event_type=TimelineEventType.DELIVERY_FAILED,
            )
        ],
    )
    assert check_delivery_stage(task_d_fail, []) == (True, False)


def test_valid_failure_kinds_covers_canonical_and_renders_compile() -> None:
    """Verify VALID_FAILURE_KINDS covers all canonical worker failures and renders clean JSON."""
    from typing import get_args

    from evaluation.provider_reliability_report import VALID_FAILURE_KINDS
    from workers.base import FailureKind

    canonical_kinds = set(get_args(FailureKind))
    missing = canonical_kinds - VALID_FAILURE_KINDS
    assert not missing, f"Missing canonical FailureKind: {missing}"

    policy = ReliabilityReportPolicy(
        as_of=datetime.now(UTC),
        window_start_at=datetime.now(UTC) - timedelta(days=90),
        window_end_at=datetime.now(UTC),
    )
    cell = _make_dummy_cell(
        "codex-native-executor", 0.5, 45.0, is_eligible=True, sample_size=10, accepted=8
    ).model_copy(
        update={
            "failure_count": 2,
            "failure_rate": 0.2,
            "typed_failures": {"compile": 2},
        }
    )
    report = ProviderReliabilityReport(
        schema_version=1,
        generated_at=datetime.now(UTC),
        status="complete",
        policy=policy,
        exclusions=TaskExclusionSummary(
            total_tasks_scanned=10,
            included_tasks_count=10,
            excluded_tasks_count=0,
            by_reason={},
        ),
        evidence_cells=[cell],
        recommendations=[],
    )
    rendered_json = render_json_report(report)
    assert '"compile": 2' in rendered_json
    assert_sanitized_report(rendered_json)
