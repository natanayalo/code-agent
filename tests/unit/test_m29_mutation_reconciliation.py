"""Unit tests for M29 feature/mutation report reconciliation and publication gates."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from evaluation.m29_mutation_cohort import (
    M29MutationCohortReconciliation,
    MutationProfileReconciliation,
)
from evaluation.m29_mutation_manifest import (
    M29MutationManifest,
    M29MutationManifestCase,
)
from evaluation.m29_mutation_reconciliation import (
    assert_manifest_matches_canonical_report,
    assert_manifest_matches_robustness_report,
    assert_manifest_matches_suite,
    assert_mutation_manifest_publication_ready,
    assert_quality_separation_integrity,
)
from evaluation.m29_mutation_suite import (
    DEFAULT_FIXTURE_REVISION,
    RAW_PAIR_DEFINITIONS,
    build_m29_mutation_suite,
)
from evaluation.provider_reliability_models import (
    DEFAULT_EXPECTED_EXECUTION_IDENTITIES,
    BudgetCoverageMetrics,
    ExecutionIdentity,
    InterventionMetrics,
    LatencyMetrics,
    ProfileCoverageSummary,
    ProviderReliabilityEvidenceCell,
    ProviderReliabilityReport,
    ProviderReliabilityRobustnessPolicy,
    ProviderReliabilityRobustnessReport,
    ReliabilityReportPolicy,
    RepairMetrics,
    StageOutcomeRates,
    TaskExclusionSummary,
    TemporalCohortResult,
    WilsonConfidenceInterval,
    WindowRecommendationResult,
)


def _make_recon(
    as_of: datetime,
    *,
    codex_eligible: int = 10,
    codex_excluded: int = 0,
    ag_eligible: int = 10,
    ag_excluded: int = 0,
) -> M29MutationCohortReconciliation:
    return M29MutationCohortReconciliation(
        as_of=as_of,
        window_start_at=as_of - timedelta(days=90),
        window_end_at=as_of,
        report_snapshot_verified=True,
        profiles={
            "codex-native-executor": MutationProfileReconciliation(
                baseline_count=0,
                scheduled_suite_count=10,
                eligible_suite_count=codex_eligible,
                excluded_suite_count=codex_excluded,
                current_count=codex_eligible,
                baseline_ids_sha256="0" * 64,
                scheduled_suite_ids_sha256="1" * 64,
                eligible_suite_ids_sha256="2" * 64,
                excluded_suite_ids_sha256="3" * 64,
                current_ids_sha256="4" * 64,
            ),
            "antigravity-native-executor": MutationProfileReconciliation(
                baseline_count=0,
                scheduled_suite_count=10,
                eligible_suite_count=ag_eligible,
                excluded_suite_count=ag_excluded,
                current_count=ag_eligible,
                baseline_ids_sha256="5" * 64,
                scheduled_suite_ids_sha256="6" * 64,
                eligible_suite_ids_sha256="7" * 64,
                excluded_suite_ids_sha256="8" * 64,
                current_ids_sha256="9" * 64,
            ),
        },
    )


def _make_manifest(
    as_of: datetime,
    *,
    codex_accepted: int = 10,
    ag_accepted: int = 10,
    codex_quality_pass: int = 10,
    ag_quality_pass: int = 10,
) -> M29MutationManifest:
    suite = build_m29_mutation_suite()
    digest = suite.compute_digest()
    cases: list[M29MutationManifestCase] = []

    codex_seen = ag_seen = 0
    for idx, raw_def in enumerate(RAW_PAIR_DEFINITIONS, start=1):
        topic, _path, _entry, _prompt, _oracle, slug = raw_def
        pair_group = f"m29-mut-pair-{idx:02d}-{slug}"
        profiles = (
            (("codex-native-executor", "codex"), ("antigravity-native-executor", "antigravity"))
            if idx % 2 == 1
            else (
                ("antigravity-native-executor", "antigravity"),
                ("codex-native-executor", "codex"),
            )
        )

        for order, (profile, provider) in enumerate(profiles, start=1):
            if provider == "codex":
                codex_seen += 1
                accepted = codex_seen <= codex_accepted
                q_pass = codex_seen <= codex_quality_pass
            else:
                ag_seen += 1
                accepted = ag_seen <= ag_accepted
                q_pass = ag_seen <= ag_quality_pass

            cases.append(
                M29MutationManifestCase(
                    case_id=f"m29-mut-{idx:02d}-{slug}-{provider}",
                    pair_group=pair_group,
                    pair_order=order,
                    topic=topic,
                    worker_profile=profile,  # type: ignore[arg-type]
                    provider=provider,  # type: ignore[arg-type]
                    terminal_status="completed" if accepted else "failed",
                    accepted=accepted,
                    failure_kind=None if accepted else "timeout",
                    report_failure_kind=None if accepted else "timeout",
                    time_to_terminal_seconds=120.0,
                    execution_identity_status="verified",
                    identity_matches=True,
                    exclusion_reason=None,
                    quality_status="pass" if q_pass else "fail",
                    acceptance_oracle_passed=q_pass,
                    regression_suite_passed=True,
                    neutral_review_passed=True,
                    evaluator_identity_status="verified",
                    evaluator_identity_matches=True,
                    quality_reason=None if q_pass else "failed acceptance oracle",
                )
            )

    return M29MutationManifest(
        suite_sha256=digest,
        build_sha="a1b2c3d4e5f67890",
        target_repository_revision=DEFAULT_FIXTURE_REVISION,
        as_of=as_of,
        advisory_report_sha256="a" * 64,
        operational_report_sha256="b" * 64,
        robustness_report_sha256="c" * 64,
        cohort_reconciliation=_make_recon(as_of),
        cases=cases,
    )


def _evidence_cell(
    profile: str,
    *,
    sample_size: int = 10,
    accepted_count: int = 10,
    typed_failures: dict[str, int] | None = None,
    verification_pass_rate: float | None = None,
    review_pass_rate: float | None = None,
) -> ProviderReliabilityEvidenceCell:
    failure_count = sample_size - accepted_count
    return ProviderReliabilityEvidenceCell(
        task_class="feature",
        profile=profile,
        mutation_mode="mutation",
        sample_size=sample_size,
        accepted_count=accepted_count,
        accepted_task_rate=round(accepted_count / sample_size, 4),
        accepted_task_rate_ci=WilsonConfidenceInterval(lower=0.5, center=0.8, upper=0.95),
        failure_count=failure_count,
        failure_rate=round(failure_count / sample_size, 4),
        stage_outcome_rates=StageOutcomeRates(
            dispatch_rate=1.0,
            execution_success_rate=1.0,
            verification_pass_rate=verification_pass_rate,
            review_pass_rate=review_pass_rate,
        ),
        typed_failures=typed_failures or ({"timeout": failure_count} if failure_count > 0 else {}),
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
        terminal_latency=LatencyMetrics(median_seconds=120.0),
        budget_field_coverage=BudgetCoverageMetrics(
            budget_reported_count=sample_size,
            budget_coverage_rate=1.0,
        ),
        is_eligible=True,
    )


def _canonical_report(
    as_of: datetime,
    *,
    codex_accepted: int = 10,
    ag_accepted: int = 10,
    codex_sample: int = 10,
    ag_sample: int = 10,
    verification_pass_rate: float | None = None,
    review_pass_rate: float | None = None,
    identities: dict[str, ExecutionIdentity] | None = None,
) -> ProviderReliabilityReport:
    policy = ReliabilityReportPolicy(
        as_of=as_of,
        window_start_at=as_of - timedelta(days=90),
        window_end_at=as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=identities or dict(DEFAULT_EXPECTED_EXECUTION_IDENTITIES),
    )
    cells = [
        _evidence_cell(
            "codex-native-executor",
            sample_size=codex_sample,
            accepted_count=codex_accepted,
            verification_pass_rate=verification_pass_rate,
            review_pass_rate=review_pass_rate,
        ),
        _evidence_cell(
            "antigravity-native-executor",
            sample_size=ag_sample,
            accepted_count=ag_accepted,
            verification_pass_rate=verification_pass_rate,
            review_pass_rate=review_pass_rate,
        ),
    ]
    return ProviderReliabilityReport(
        generated_at=as_of,
        status="complete",
        policy=policy,
        profile_coverage=[
            ProfileCoverageSummary(
                profile="codex-native-executor",
                mutation_mode="mutation",
                has_evidence=True,
                sample_size=codex_sample,
                is_eligible=True,
            ),
            ProfileCoverageSummary(
                profile="antigravity-native-executor",
                mutation_mode="mutation",
                has_evidence=True,
                sample_size=ag_sample,
                is_eligible=True,
            ),
        ],
        exclusions=TaskExclusionSummary(
            total_tasks_scanned=20,
            included_tasks_count=20,
            excluded_tasks_count=0,
            by_reason={},
        ),
        evidence_cells=cells,
        recommendations=[],
    )


def _robustness_report(as_of: datetime) -> ProviderReliabilityRobustnessReport:
    policy = ProviderReliabilityRobustnessPolicy(
        as_of=as_of,
        window_start_at=as_of - timedelta(days=90),
        window_end_at=as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(DEFAULT_EXPECTED_EXECUTION_IDENTITIES),
    )
    return ProviderReliabilityRobustnessReport(
        generated_at=as_of,
        status="complete",
        policy=policy,
        exclusions=TaskExclusionSummary(
            total_tasks_scanned=20,
            included_tasks_count=20,
            excluded_tasks_count=0,
            by_reason={},
        ),
        windows=[
            WindowRecommendationResult(
                window_days=90,
                window_start_at=as_of - timedelta(days=90),
                window_end_at=as_of,
                included_tasks_count=20,
                recommendations=[],
            )
        ],
        temporal_cohorts=[
            TemporalCohortResult(
                cohort_name="recent",
                window_start_at=as_of - timedelta(days=45),
                window_end_at=as_of,
                included_tasks_count=20,
                recommendations=[],
            )
        ],
        bootstrap_results=[],
    )


def test_manifest_matches_suite_happy_path() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    suite = build_m29_mutation_suite()
    assert_manifest_matches_suite(manifest, suite)


def test_manifest_matches_suite_mismatches_rejected() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    suite = build_m29_mutation_suite()

    bad_name = manifest.model_copy(update={"suite_name": "m29-wrong-suite"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="suite_name .* does not match"):
        assert_manifest_matches_suite(bad_name, suite)

    bad_digest = manifest.model_copy(update={"suite_sha256": "0" * 64})
    with pytest.raises(ValueError, match="suite_sha256 .* does not match"):
        assert_manifest_matches_suite(bad_digest, suite)

    bad_rev = manifest.model_copy(update={"target_repository_revision": "f" * 40})
    with pytest.raises(ValueError, match="target_repository_revision .* does not match"):
        assert_manifest_matches_suite(bad_rev, suite)

    # Case mismatch
    altered_cases = list(manifest.cases)
    altered_cases[0] = altered_cases[0].model_copy(update={"topic": "tampered_topic"})
    bad_case = manifest.model_copy(update={"cases": altered_cases})
    with pytest.raises(ValueError, match="topic mismatch"):
        assert_manifest_matches_suite(bad_case, suite)

    # Evaluator identity mismatch
    altered_eval = list(manifest.cases)
    altered_eval[0] = altered_eval[0].model_copy(update={"evaluator_identity_matches": False})
    bad_eval = manifest.model_copy(update={"cases": altered_eval})
    with pytest.raises(ValueError, match="evaluator identity did not match suite evaluator"):
        assert_manifest_matches_suite(bad_eval, suite)


def test_quality_separation_integrity_passes_with_mixed_outcomes() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    # 8 completed, 6 quality pass
    manifest = _make_manifest(
        as_of,
        codex_accepted=8,
        ag_accepted=9,
        codex_quality_pass=6,
        ag_quality_pass=7,
    )
    assert_quality_separation_integrity(manifest)


def test_quality_separation_tampering_rejected() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)

    # Tampering: completed case marked not accepted
    altered = list(manifest.cases)
    altered[0] = altered[0].model_copy(update={"accepted": False})
    with pytest.raises(ValueError, match="tampering detected: completed eligible case"):
        assert_quality_separation_integrity(manifest.model_copy(update={"cases": altered}))

    # Tampering: failed case marked accepted
    manifest2 = _make_manifest(as_of, codex_accepted=5)
    altered2 = list(manifest2.cases)
    failed_idx = next(i for i, c in enumerate(altered2) if not c.accepted)
    altered2[failed_idx] = altered2[failed_idx].model_copy(update={"accepted": True})
    with pytest.raises(ValueError, match="tampering detected: failed case"):
        assert_quality_separation_integrity(manifest2.model_copy(update={"cases": altered2}))


def test_canonical_report_matching_happy_path() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of, codex_accepted=8, ag_accepted=9)
    report = _canonical_report(as_of, codex_accepted=8, ag_accepted=9)
    assert_manifest_matches_canonical_report(manifest, report)


def test_canonical_report_policy_mismatches_rejected() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    report = _canonical_report(as_of)

    # Cutoff mismatch
    bad_policy = report.policy.model_copy(update={"as_of": as_of + timedelta(days=1)})
    with pytest.raises(ValueError, match="canonical report policy as_of"):
        assert_manifest_matches_canonical_report(
            manifest, report.model_copy(update={"policy": bad_policy})
        )

    # Scope mismatch
    bad_scope = report.policy.model_copy(update={"evidence_scope": "operational"})
    with pytest.raises(ValueError, match="evidence_scope must be 'current_execution_cohort'"):
        assert_manifest_matches_canonical_report(
            manifest, report.model_copy(update={"policy": bad_scope})
        )

    # Identity mismatch
    bad_identities = dict(report.policy.expected_execution_identities)
    bad_identities["codex-native-executor"] = ExecutionIdentity(
        provider="codex", model="gpt-wrong-model", reasoning_effort="high"
    )
    bad_id_policy = report.policy.model_copy(
        update={"expected_execution_identities": bad_identities}
    )
    with pytest.raises(ValueError, match="execution identity mismatch"):
        assert_manifest_matches_canonical_report(
            manifest, report.model_copy(update={"policy": bad_id_policy})
        )


def test_canonical_report_cell_mismatches_rejected() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of, codex_accepted=10, ag_accepted=10)

    # Missing cell
    empty_cells_report = _canonical_report(as_of).model_copy(update={"evidence_cells": []})
    with pytest.raises(ValueError, match="missing evidence cell"):
        assert_manifest_matches_canonical_report(manifest, empty_cells_report)

    # Sample size mismatch
    mismatched_sample = _canonical_report(as_of, codex_sample=9, codex_accepted=9)
    with pytest.raises(ValueError, match="sample size .* does not match"):
        assert_manifest_matches_canonical_report(manifest, mismatched_sample)

    # In-task verification stage rate presence rejected
    with pytest.raises(ValueError, match="has verification_pass_rate"):
        assert_manifest_matches_canonical_report(
            manifest, _canonical_report(as_of, verification_pass_rate=1.0)
        )

    # In-task review stage rate presence rejected
    with pytest.raises(ValueError, match="has review_pass_rate"):
        assert_manifest_matches_canonical_report(
            manifest, _canonical_report(as_of, review_pass_rate=1.0)
        )

    # Baseline cell with non-null stage rate mismatch
    base_v_report = _canonical_report(as_of, verification_pass_rate=0.4, review_pass_rate=0.3)
    bad_v_report = _canonical_report(as_of, verification_pass_rate=0.6, review_pass_rate=0.3)
    with pytest.raises(ValueError, match="has verification_pass_rate"):
        assert_manifest_matches_canonical_report(
            manifest, bad_v_report, baseline_report=base_v_report
        )

    # In-task repairs presence rejected
    bad_repair_cell = _evidence_cell("codex-native-executor")
    bad_repair_cell.repairs.verifier_repairs_count = 1
    bad_repair_cell.repairs.total_repaired_tasks = 1
    bad_repair_report = _canonical_report(as_of).model_copy(
        update={
            "evidence_cells": [
                bad_repair_cell,
                _evidence_cell("antigravity-native-executor"),
            ]
        }
    )
    with pytest.raises(ValueError, match="repaired tasks"):
        assert_manifest_matches_canonical_report(manifest, bad_repair_report)

    # Accepted count mismatch
    with pytest.raises(ValueError, match="accepted count mismatch"):
        assert_manifest_matches_canonical_report(
            manifest, _canonical_report(as_of, codex_accepted=9)
        )


def test_robustness_report_matching() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    robustness = _robustness_report(as_of)
    assert_manifest_matches_robustness_report(manifest, robustness)

    bad_as_of = robustness.policy.model_copy(update={"as_of": as_of + timedelta(hours=1)})
    with pytest.raises(ValueError, match="robustness report policy as_of"):
        assert_manifest_matches_robustness_report(
            manifest, robustness.model_copy(update={"policy": bad_as_of})
        )


def test_publication_ready_full_gate() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    suite = build_m29_mutation_suite()
    canonical = _canonical_report(as_of)
    robustness = _robustness_report(as_of)

    canonical_json = canonical.model_dump_json()
    robustness_json = robustness.model_dump_json()
    canonical_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    robustness_hash = hashlib.sha256(robustness_json.encode("utf-8")).hexdigest()

    manifest_with_hashes = manifest.model_copy(
        update={
            "advisory_report_sha256": canonical_hash,
            "robustness_report_sha256": robustness_hash,
        }
    )

    assert_mutation_manifest_publication_ready(
        manifest_with_hashes,
        suite,
        canonical,
        robustness,
        canonical_report_json=canonical_json,
        robustness_report_json=robustness_json,
    )

    # Rejection on mismatched report JSON
    with pytest.raises(ValueError, match="canonical report JSON SHA-256 does not match"):
        assert_mutation_manifest_publication_ready(
            manifest_with_hashes,
            suite,
            canonical,
            robustness,
            canonical_report_json='{"tampered": true}',
            robustness_report_json=robustness_json,
        )


def test_manifest_matches_suite_field_mismatches() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    suite = build_m29_mutation_suite()

    # Case count mismatch
    short_suite = suite.model_copy(update={"cases": suite.cases[:19]})
    manifest_short = manifest.model_copy(update={"suite_sha256": short_suite.compute_digest()})
    with pytest.raises(ValueError, match="manifest case count .* does not match"):
        assert_manifest_matches_suite(manifest_short, short_suite)

    # Case ID mismatch
    alt_cases = list(manifest.cases)
    alt_cases[0] = alt_cases[0].model_copy(update={"case_id": "m29-mut-wrong-id"})
    with pytest.raises(ValueError, match="ID mismatch"):
        assert_manifest_matches_suite(manifest.model_copy(update={"cases": alt_cases}), suite)

    # Pair group mismatch
    alt_group = list(manifest.cases)
    alt_group[0] = alt_group[0].model_copy(update={"pair_group": "m29-mut-pair-wrong"})
    with pytest.raises(ValueError, match="pair_group mismatch"):
        assert_manifest_matches_suite(manifest.model_copy(update={"cases": alt_group}), suite)

    # Pair order mismatch
    alt_order = list(manifest.cases)
    alt_order[0] = alt_order[0].model_copy(update={"pair_order": 2})
    with pytest.raises(ValueError, match="pair_order mismatch"):
        assert_manifest_matches_suite(manifest.model_copy(update={"cases": alt_order}), suite)

    # Worker profile mismatch
    alt_prof = list(manifest.cases)
    alt_prof[0] = alt_prof[0].model_copy(
        update={"worker_profile": "antigravity-native-executor", "provider": "antigravity"}
    )
    with pytest.raises(ValueError, match="worker_profile mismatch"):
        assert_manifest_matches_suite(manifest.model_copy(update={"cases": alt_prof}), suite)


def test_quality_separation_tampering_checks() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)

    # Tampering: quality pass despite failing regression suite
    alt1 = list(manifest.cases)
    alt1[0] = alt1[0].model_copy(update={"regression_suite_passed": False})
    with pytest.raises(ValueError, match="quality_status is 'pass' despite failing"):
        assert_quality_separation_integrity(manifest.model_copy(update={"cases": alt1}))

    # Tampering: quality pass with unverified evaluator
    alt2 = list(manifest.cases)
    alt2[0] = alt2[0].model_copy(update={"evaluator_identity_status": "unavailable"})
    with pytest.raises(ValueError, match="with unverified or mismatched evaluator identity"):
        assert_quality_separation_integrity(manifest.model_copy(update={"cases": alt2}))


def test_canonical_report_more_policy_and_cell_mismatches() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of, codex_accepted=8, ag_accepted=9)
    report = _canonical_report(as_of, codex_accepted=8, ag_accepted=9)

    # Window start mismatch
    bad_window = report.policy.model_copy(update={"window_start_at": as_of - timedelta(days=60)})
    with pytest.raises(ValueError, match="canonical report policy window does not match"):
        assert_manifest_matches_canonical_report(
            manifest, report.model_copy(update={"policy": bad_window})
        )

    # Min samples mismatch
    bad_min = report.policy.model_copy(update={"min_samples": 5})
    with pytest.raises(ValueError, match="canonical report min_samples must be 10"):
        assert_manifest_matches_canonical_report(
            manifest, report.model_copy(update={"policy": bad_min})
        )

    # Failure count mismatch
    bad_cell = _evidence_cell("codex-native-executor", sample_size=10, accepted_count=8)
    bad_cell = bad_cell.model_copy(update={"failure_count": 5})
    bad_report = report.model_copy(update={"evidence_cells": [bad_cell, report.evidence_cells[1]]})
    with pytest.raises(ValueError, match="failure count mismatch"):
        assert_manifest_matches_canonical_report(manifest, bad_report)

    # Typed failures mismatch
    bad_typed_cell = _evidence_cell("codex-native-executor", sample_size=10, accepted_count=8)
    bad_typed_cell = bad_typed_cell.model_copy(update={"typed_failures": {"wrong_failure": 2}})
    bad_report2 = report.model_copy(
        update={"evidence_cells": [bad_typed_cell, report.evidence_cells[1]]}
    )
    with pytest.raises(ValueError, match="typed failures mismatch"):
        assert_manifest_matches_canonical_report(manifest, bad_report2)


def test_canonical_report_baseline_delta_reconciliation() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of, codex_accepted=8, ag_accepted=9)

    baseline_report = _canonical_report(
        as_of - timedelta(days=30),
        codex_accepted=5,
        ag_accepted=5,
        codex_sample=5,
        ag_sample=5,
    )

    combined_report = _canonical_report(
        as_of,
        codex_accepted=13,
        ag_accepted=14,
        codex_sample=15,
        ag_sample=15,
    )
    recon_profiles = dict(manifest.cohort_reconciliation.profiles)
    p_codex = recon_profiles["codex-native-executor"]
    recon_profiles["codex-native-executor"] = MutationProfileReconciliation(
        baseline_count=5,
        scheduled_suite_count=p_codex.scheduled_suite_count,
        eligible_suite_count=p_codex.eligible_suite_count,
        excluded_suite_count=p_codex.excluded_suite_count,
        current_count=15,
        baseline_ids_sha256=p_codex.baseline_ids_sha256,
        scheduled_suite_ids_sha256=p_codex.scheduled_suite_ids_sha256,
        eligible_suite_ids_sha256=p_codex.eligible_suite_ids_sha256,
        excluded_suite_ids_sha256=p_codex.excluded_suite_ids_sha256,
        current_ids_sha256=p_codex.current_ids_sha256,
    )
    p_ag = recon_profiles["antigravity-native-executor"]
    recon_profiles["antigravity-native-executor"] = MutationProfileReconciliation(
        baseline_count=5,
        scheduled_suite_count=p_ag.scheduled_suite_count,
        eligible_suite_count=p_ag.eligible_suite_count,
        excluded_suite_count=p_ag.excluded_suite_count,
        current_count=15,
        baseline_ids_sha256=p_ag.baseline_ids_sha256,
        scheduled_suite_ids_sha256=p_ag.scheduled_suite_ids_sha256,
        eligible_suite_ids_sha256=p_ag.eligible_suite_ids_sha256,
        excluded_suite_ids_sha256=p_ag.excluded_suite_ids_sha256,
        current_ids_sha256=p_ag.current_ids_sha256,
    )
    reconciled_recon = M29MutationCohortReconciliation(
        as_of=as_of,
        window_start_at=manifest.cohort_reconciliation.window_start_at,
        window_end_at=manifest.cohort_reconciliation.window_end_at,
        report_snapshot_verified=True,
        profiles=recon_profiles,
    )
    reconciled_manifest = manifest.model_copy(update={"cohort_reconciliation": reconciled_recon})

    assert_manifest_matches_canonical_report(
        reconciled_manifest, combined_report, baseline_report=baseline_report
    )


def test_robustness_report_mismatches_and_publication_ready_hashes() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    robustness = _robustness_report(as_of)

    # Robustness window mismatch
    bad_window = robustness.policy.model_copy(
        update={"window_start_at": as_of - timedelta(days=60)}
    )
    with pytest.raises(ValueError, match="robustness report policy window does not match"):
        assert_manifest_matches_robustness_report(
            manifest, robustness.model_copy(update={"policy": bad_window})
        )

    # Robustness scope mismatch
    bad_scope = robustness.policy.model_copy(update={"evidence_scope": "operational"})
    with pytest.raises(ValueError, match="evidence_scope must be 'current_execution_cohort'"):
        assert_manifest_matches_robustness_report(
            manifest, robustness.model_copy(update={"policy": bad_scope})
        )

    # Robustness identity mismatch
    bad_id = dict(robustness.policy.expected_execution_identities)
    bad_id["codex-native-executor"] = ExecutionIdentity(provider="codex", model="wrong")
    bad_pol = robustness.policy.model_copy(update={"expected_execution_identities": bad_id})
    with pytest.raises(ValueError, match="execution identity mismatch"):
        assert_manifest_matches_robustness_report(
            manifest, robustness.model_copy(update={"policy": bad_pol})
        )

    # Publication ready robustness & operational hash mismatch
    suite = build_m29_mutation_suite()
    canonical = _canonical_report(as_of)
    with pytest.raises(ValueError, match="robustness report JSON SHA-256 does not match"):
        assert_mutation_manifest_publication_ready(
            manifest,
            suite,
            canonical,
            robustness,
            robustness_report_json='{"robustness": "bad"}',
        )

    with pytest.raises(ValueError, match="operational report JSON SHA-256 does not match"):
        assert_mutation_manifest_publication_ready(
            manifest,
            suite,
            canonical,
            robustness,
            operational_report_json='{"operational": "bad"}',
        )


def test_manifest_matches_canonical_report_no_baseline_report_with_baseline_count() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    profiles = dict(manifest.cohort_reconciliation.profiles)
    profiles["codex-native-executor"] = replace(
        profiles["codex-native-executor"], baseline_count=5, current_count=15
    )
    recon = replace(manifest.cohort_reconciliation, profiles=profiles)
    manifest_with_baseline_recon = manifest.model_copy(update={"cohort_reconciliation": recon})
    report = _canonical_report(as_of, codex_sample=15)
    with pytest.raises(ValueError, match="baseline report is required for codex-native-executor"):
        assert_manifest_matches_canonical_report(
            manifest_with_baseline_recon, report, baseline_report=None
        )


def test_manifest_matches_canonical_report_baseline_sample_size_mismatch() -> None:
    as_of = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    manifest = _make_manifest(as_of)
    profiles = dict(manifest.cohort_reconciliation.profiles)
    profiles["codex-native-executor"] = replace(
        profiles["codex-native-executor"], baseline_count=5, current_count=15
    )
    recon = replace(manifest.cohort_reconciliation, profiles=profiles)
    manifest_with_baseline_recon = manifest.model_copy(update={"cohort_reconciliation": recon})
    report = _canonical_report(as_of, codex_sample=15)
    baseline_rep = _canonical_report(as_of - timedelta(days=1), codex_sample=4, codex_accepted=4)
    with pytest.raises(ValueError, match="baseline cell sample size"):
        assert_manifest_matches_canonical_report(
            manifest_with_baseline_recon, report, baseline_report=baseline_rep
        )
