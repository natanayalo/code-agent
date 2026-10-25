"""Reconciliation and publication gates for M29 feature/mutation reports."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from evaluation.m29_mutation_cohort import M29_MUTATION_PROFILES, MutationProfileReconciliation
from evaluation.m29_mutation_manifest import (
    M29MutationManifest,
    M29MutationManifestCase,
    assert_sanitized_mutation_manifest,
)
from evaluation.m29_mutation_suite import (
    M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES,
    M29MutationSuite,
)
from evaluation.provider_reliability_models import (
    ProviderReliabilityEvidenceCell,
    ProviderReliabilityReport,
    ProviderReliabilityRobustnessPolicy,
    ProviderReliabilityRobustnessReport,
    ReliabilityReportPolicy,
)


def assert_manifest_matches_suite(
    manifest: M29MutationManifest,
    suite: M29MutationSuite,
) -> None:
    """Require exact binding between sanitized manifest cases and frozen suite."""
    if manifest.suite_name != suite.suite_name:
        raise ValueError(
            f"manifest suite_name '{manifest.suite_name}' does not match "
            f"suite suite_name '{suite.suite_name}'"
        )
    expected_digest = suite.compute_digest()
    if manifest.suite_sha256 != expected_digest:
        raise ValueError(
            f"manifest suite_sha256 '{manifest.suite_sha256}' does not match "
            f"suite digest '{expected_digest}'"
        )
    if manifest.target_repository_revision != suite.target_repository_revision:
        raise ValueError(
            f"manifest target_repository_revision '{manifest.target_repository_revision}' "
            f"does not match suite '{suite.target_repository_revision}'"
        )
    if len(manifest.cases) != len(suite.cases):
        raise ValueError(
            f"manifest case count ({len(manifest.cases)}) does not match "
            f"suite case count ({len(suite.cases)})"
        )

    _validate_manifest_suite_cases(manifest.cases, suite.cases)


def _validate_manifest_suite_cases(
    manifest_cases: list[M29MutationManifestCase],
    suite_cases: list[object],
) -> None:
    for idx, (m_case, s_case) in enumerate(zip(manifest_cases, suite_cases, strict=True)):
        if m_case.case_id != getattr(s_case, "case_id"):
            raise ValueError(
                f"case at index {idx} ID mismatch: manifest '{m_case.case_id}' != "
                f"suite '{getattr(s_case, 'case_id')}'"
            )
        if m_case.pair_group != getattr(s_case, "pair_group"):
            raise ValueError(
                f"case '{m_case.case_id}' pair_group mismatch: '{m_case.pair_group}' != "
                f"'{getattr(s_case, 'pair_group')}'"
            )
        if m_case.pair_order != getattr(s_case, "pair_order"):
            raise ValueError(
                f"case '{m_case.case_id}' pair_order mismatch: {m_case.pair_order} != "
                f"{getattr(s_case, 'pair_order')}"
            )
        if m_case.topic != getattr(s_case, "topic"):
            raise ValueError(
                f"case '{m_case.case_id}' topic mismatch: '{m_case.topic}' != "
                f"'{getattr(s_case, 'topic')}'"
            )
        if m_case.worker_profile != getattr(s_case, "worker_profile"):
            raise ValueError(
                f"case '{m_case.case_id}' worker_profile mismatch: '{m_case.worker_profile}' != "
                f"'{getattr(s_case, 'worker_profile')}'"
            )
        s_eval = getattr(s_case, "neutral_evaluator", None)
        if s_eval is not None and m_case.evaluator_identity_status == "verified":
            if not m_case.evaluator_identity_matches:
                raise ValueError(
                    f"case '{m_case.case_id}' evaluator identity did not match suite evaluator"
                )


def assert_quality_separation_integrity(manifest: M29MutationManifest) -> None:
    """Verify that independent feature quality does not alter canonical task terminal outcomes."""
    for case in manifest.cases:
        if case.terminal_status == "completed" and case.exclusion_reason is None:
            if not case.accepted:
                raise ValueError(
                    f"tampering detected: completed eligible case '{case.case_id}' was "
                    "marked not accepted"
                )
        elif case.terminal_status == "failed":
            if case.accepted:
                raise ValueError(
                    f"tampering detected: failed case '{case.case_id}' was marked accepted"
                )

        if case.quality_status == "pass":
            if not (
                case.acceptance_oracle_passed
                and case.regression_suite_passed
                and case.neutral_review_passed
            ):
                raise ValueError(
                    f"tampering detected: case '{case.case_id}' quality_status is 'pass' "
                    "despite failing external quality checks"
                )
            if not case.evaluator_identity_matches or case.evaluator_identity_status != "verified":
                raise ValueError(
                    f"tampering detected: case '{case.case_id}' quality_status is 'pass' "
                    "with unverified or mismatched evaluator identity"
                )


def _validate_canonical_policy(
    manifest: M29MutationManifest,
    policy: ReliabilityReportPolicy,
) -> None:
    if policy.as_of != manifest.as_of:
        raise ValueError(
            f"canonical report policy as_of ({policy.as_of.isoformat()}) does not match "
            f"manifest as_of ({manifest.as_of.isoformat()})"
        )
    expected_start = manifest.as_of - timedelta(days=90)
    if policy.window_start_at != expected_start or policy.window_end_at != manifest.as_of:
        raise ValueError("canonical report policy window does not match manifest 90-day window")
    if policy.evidence_scope != "current_execution_cohort":
        raise ValueError(
            f"canonical report evidence_scope must be 'current_execution_cohort', "
            f"got '{policy.evidence_scope}'"
        )
    if policy.min_samples != 10:
        raise ValueError(f"canonical report min_samples must be 10, got {policy.min_samples}")

    for profile in M29_MUTATION_PROFILES:
        expected_id = M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES[profile]
        actual_id = policy.expected_execution_identities.get(profile)
        if actual_id != expected_id:
            raise ValueError(
                f"canonical report execution identity mismatch for {profile}: "
                f"expected {expected_id}, got {actual_id}"
            )


def assert_manifest_matches_canonical_report(
    manifest: M29MutationManifest,
    report: ProviderReliabilityReport,
    *,
    baseline_report: ProviderReliabilityReport | None = None,
) -> None:
    """Require exact alignment between manifest, cohort reconciliation, and canonical cells."""
    _validate_canonical_policy(manifest, report.policy)

    report_cells = {(c.task_class, c.profile, c.mutation_mode): c for c in report.evidence_cells}
    baseline_cells = (
        {(c.task_class, c.profile, c.mutation_mode): c for c in baseline_report.evidence_cells}
        if baseline_report is not None
        else {}
    )

    for profile in M29_MUTATION_PROFILES:
        cell = report_cells.get(("feature", profile, "mutation"))
        if cell is None:
            raise ValueError(
                f"canonical report is missing evidence cell for feature/{profile}/mutation"
            )
        b_cell = baseline_cells.get(("feature", profile, "mutation"))
        recon_profile = manifest.cohort_reconciliation.profiles[profile]
        _validate_mutation_cell(manifest, cell, b_cell, profile, recon_profile)


def _validate_mutation_cell(
    manifest: M29MutationManifest,
    cell: ProviderReliabilityEvidenceCell,
    baseline_cell: ProviderReliabilityEvidenceCell | None,
    profile: str,
    recon_profile: MutationProfileReconciliation,
) -> None:
    if cell.sample_size != recon_profile.current_count:
        raise ValueError(
            f"canonical report sample size ({cell.sample_size}) does not match "
            f"cohort reconciliation ({recon_profile.current_count}) for {profile}"
        )
    exp_total_rep = baseline_cell.repairs.total_repaired_tasks if baseline_cell else 0
    exp_verifier_rep = baseline_cell.repairs.verifier_repairs_count if baseline_cell else 0
    exp_review_rep = baseline_cell.repairs.review_repairs_count if baseline_cell else 0

    act_rep = cell.repairs
    if act_rep.total_repaired_tasks != exp_total_rep:
        raise ValueError(
            f"canonical report feature/{profile}/mutation repaired tasks "
            f"({act_rep.total_repaired_tasks}) != baseline ({exp_total_rep}); "
            "suite tasks must not record in-task repairs"
        )
    if act_rep.verifier_repairs_count != exp_verifier_rep:
        raise ValueError(
            f"canonical report feature/{profile}/mutation verifier repairs "
            f"({act_rep.verifier_repairs_count}) != baseline ({exp_verifier_rep}); "
            "suite tasks must not record in-task verifier repairs"
        )
    if act_rep.review_repairs_count != exp_review_rep:
        raise ValueError(
            f"canonical report feature/{profile}/mutation review repairs "
            f"({act_rep.review_repairs_count}) != baseline ({exp_review_rep}); "
            "suite tasks must not record in-task review repairs"
        )

    base_v_rate = (
        baseline_cell.stage_outcome_rates.verification_pass_rate if baseline_cell else None
    )
    if cell.stage_outcome_rates.verification_pass_rate != base_v_rate:
        raise ValueError(
            f"canonical report feature/{profile}/mutation has verification_pass_rate "
            f"({cell.stage_outcome_rates.verification_pass_rate}) != baseline ({base_v_rate}); "
            "external quality must not be backfilled into in-task stages"
        )
    base_r_rate = baseline_cell.stage_outcome_rates.review_pass_rate if baseline_cell else None
    if cell.stage_outcome_rates.review_pass_rate != base_r_rate:
        raise ValueError(
            f"canonical report feature/{profile}/mutation has review_pass_rate "
            f"({cell.stage_outcome_rates.review_pass_rate}) != baseline ({base_r_rate}); "
            "external quality must not be backfilled into in-task stages"
        )

    _validate_cell_counts_and_failures(manifest, cell, baseline_cell, profile, recon_profile)


def _validate_cell_counts_and_failures(
    manifest: M29MutationManifest,
    cell: ProviderReliabilityEvidenceCell,
    baseline_cell: ProviderReliabilityEvidenceCell | None,
    profile: str,
    recon_profile: MutationProfileReconciliation,
) -> None:
    p_included = [
        c for c in manifest.cases if c.worker_profile == profile and c.exclusion_reason is None
    ]
    suite_accepted = sum(c.accepted for c in p_included)
    suite_failed = sum(not c.accepted for c in p_included)

    if baseline_cell is not None:
        if baseline_cell.sample_size != recon_profile.baseline_count:
            raise ValueError(
                f"baseline cell sample size ({baseline_cell.sample_size}) does not match cohort "
                f"reconciliation baseline_count ({recon_profile.baseline_count}) for {profile}"
            )
        expected_accepted = baseline_cell.accepted_count + suite_accepted
        expected_failed = baseline_cell.failure_count + suite_failed
        expected_typed = dict(baseline_cell.typed_failures)
    elif recon_profile.baseline_count == 0:
        expected_accepted = suite_accepted
        expected_failed = suite_failed
        expected_typed = {}
    else:
        raise ValueError(
            f"baseline report is required for {profile} when baseline_count > 0 "
            f"({recon_profile.baseline_count})"
        )

    if cell.accepted_count != expected_accepted:
        raise ValueError(
            f"accepted count mismatch for {profile}: report={cell.accepted_count}, "
            f"expected={expected_accepted}"
        )
    if cell.failure_count != expected_failed:
        raise ValueError(
            f"failure count mismatch for {profile}: report={cell.failure_count}, "
            f"expected={expected_failed}"
        )

    for c in p_included:
        if not c.accepted and c.report_failure_kind:
            expected_typed[c.report_failure_kind] = expected_typed.get(c.report_failure_kind, 0) + 1

    filtered_expected = {k: v for k, v in expected_typed.items() if v > 0}
    if cell.typed_failures != filtered_expected:
        raise ValueError(
            f"typed failures mismatch for {profile}: report={cell.typed_failures}, "
            f"expected={filtered_expected}"
        )


def assert_manifest_matches_robustness_report(
    manifest: M29MutationManifest,
    report: ProviderReliabilityRobustnessReport,
) -> None:
    """Require exact alignment between manifest cutoff, policy, and robustness report."""
    policy: ProviderReliabilityRobustnessPolicy = report.policy
    if policy.as_of != manifest.as_of:
        raise ValueError(
            f"robustness report policy as_of ({policy.as_of.isoformat()}) does not match "
            f"manifest as_of ({manifest.as_of.isoformat()})"
        )
    expected_start = manifest.as_of - timedelta(days=90)
    if policy.window_start_at != expected_start or policy.window_end_at != manifest.as_of:
        raise ValueError("robustness report policy window does not match manifest 90-day window")
    if policy.evidence_scope != "current_execution_cohort":
        raise ValueError(
            f"robustness report evidence_scope must be 'current_execution_cohort', "
            f"got '{policy.evidence_scope}'"
        )

    for profile in M29_MUTATION_PROFILES:
        expected_id = M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES[profile]
        actual_id = policy.expected_execution_identities.get(profile)
        if actual_id != expected_id:
            raise ValueError(
                f"robustness report execution identity mismatch for {profile}: "
                f"expected {expected_id}, got {actual_id}"
            )


def assert_mutation_manifest_publication_ready(
    manifest: M29MutationManifest,
    suite: M29MutationSuite,
    canonical_report: ProviderReliabilityReport,
    robustness_report: ProviderReliabilityRobustnessReport,
    *,
    baseline_report: ProviderReliabilityReport | None = None,
    canonical_report_json: str | None = None,
    robustness_report_json: str | None = None,
    operational_report_json: str | None = None,
) -> None:
    """Full-gate verification: schema, sanitization, suite binding, quality separation, reports."""
    assert_sanitized_mutation_manifest(manifest.model_dump(mode="json"))
    assert_manifest_matches_suite(manifest, suite)
    assert_quality_separation_integrity(manifest)
    assert_manifest_matches_canonical_report(
        manifest, canonical_report, baseline_report=baseline_report
    )
    assert_manifest_matches_robustness_report(manifest, robustness_report)

    if canonical_report_json is not None:
        digest = hashlib.sha256(canonical_report_json.encode("utf-8")).hexdigest()
        if manifest.advisory_report_sha256 != digest:
            raise ValueError(
                "canonical report JSON SHA-256 does not match manifest advisory_report_sha256"
            )

    if robustness_report_json is not None:
        digest = hashlib.sha256(robustness_report_json.encode("utf-8")).hexdigest()
        if manifest.robustness_report_sha256 != digest:
            raise ValueError(
                "robustness report JSON SHA-256 does not match manifest robustness_report_sha256"
            )

    if operational_report_json is not None:
        digest = hashlib.sha256(operational_report_json.encode("utf-8")).hexdigest()
        if manifest.operational_report_sha256 != digest:
            raise ValueError(
                "operational report JSON SHA-256 does not match manifest operational_report_sha256"
            )


__all__ = [
    "assert_manifest_matches_canonical_report",
    "assert_manifest_matches_robustness_report",
    "assert_manifest_matches_suite",
    "assert_mutation_manifest_publication_ready",
    "assert_quality_separation_integrity",
]
