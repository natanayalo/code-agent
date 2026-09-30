"""Exact provenance reconciliation for the Wave 4 cumulative report."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import timedelta

from evaluation.m29_wave4_paired import (
    Wave4Manifest,
    Wave4ManifestCase,
    Wave4SupplementalObservation,
)
from evaluation.provider_reliability_models import (
    M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES,
    ProviderReliabilityEvidenceCell,
    ProviderReliabilityReport,
    ReliabilityReportPolicy,
    TaskClassRecommendation,
)

_PROVIDERS = ("codex", "antigravity")


def _report_policy(manifest: Wave4Manifest, report: ProviderReliabilityReport) -> None:
    expected = ReliabilityReportPolicy(
        as_of=manifest.as_of,
        window_start_at=manifest.as_of - timedelta(days=90),
        window_end_at=manifest.as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES),
    )
    if report.policy != expected:
        raise ValueError("canonical report policy does not match Wave 4 manifest")
    if (
        report.policy.expected_execution_identities
        != M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES
    ):
        raise ValueError("canonical report execution identities do not match Wave 4 policy")


def _baseline_policy(manifest: Wave4Manifest, report: ProviderReliabilityReport) -> None:
    policy = report.policy
    if policy.evidence_scope != "current_execution_cohort":
        raise ValueError("Wave 4 baseline report must use the current execution cohort")
    if policy.expected_execution_identities != M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES:
        raise ValueError("Wave 4 baseline report execution identities do not match Wave 4 policy")
    if policy.as_of > manifest.as_of:
        raise ValueError("Wave 4 baseline report must predate the Wave 4 report")


def _cell_map(
    cells: Sequence[ProviderReliabilityEvidenceCell],
) -> dict[tuple[str, str, str], ProviderReliabilityEvidenceCell]:
    return {(cell.task_class, cell.profile, cell.mutation_mode): cell for cell in cells}


def _validate_exact_task_sets(
    baseline_ids: Mapping[str, Collection[str]],
    cell_ids: Mapping[str, Collection[str]],
    supplemental_ids: Mapping[str, Collection[str]],
    wave4_ids: Mapping[str, Collection[str]],
    extractor_ids: Collection[str],
) -> None:
    maps = (baseline_ids, cell_ids, supplemental_ids, wave4_ids)
    if any(set(task_map) != set(_PROVIDERS) for task_map in maps):
        raise ValueError("exact task-set reconciliation requires both provider cells")
    baseline = set().union(*(set(values) for values in baseline_ids.values()))
    supplemental = set().union(*(set(values) for values in supplemental_ids.values()))
    wave4 = set().union(*(set(values) for values in wave4_ids.values()))
    if baseline & wave4 or baseline & supplemental or wave4 & supplemental:
        raise ValueError("Wave 3, Wave 4, and supplemental task sets must be disjoint")
    missing_wave4 = wave4 - set(extractor_ids)
    if missing_wave4:
        raise ValueError(
            "canonical extractor omitted eligible Wave 4 task observations: "
            f"{len(missing_wave4)} task(s)"
        )


def _baseline_cell(
    report: ProviderReliabilityReport, profile: str
) -> ProviderReliabilityEvidenceCell:
    cell = next(
        (
            item
            for item in report.evidence_cells
            if (item.task_class, item.profile, item.mutation_mode)
            == ("investigation", profile, "read_only")
        ),
        None,
    )
    if cell is None:
        raise ValueError(f"Wave 3 baseline is missing investigation/{profile}")
    return cell


def _expected_typed_failures(
    baseline: ProviderReliabilityEvidenceCell,
    cases: Sequence[Wave4ManifestCase],
    supplemental: Sequence[Wave4SupplementalObservation],
    profile: str,
) -> dict[str, int]:
    result = dict(baseline.typed_failures)
    failed = [case for case in (*cases, *supplemental) if not case.accepted]
    for case in failed:
        failure_kind = case.report_failure_kind
        if failure_kind is None:
            raise ValueError(
                f"eligible failed investigation/{profile} observation lacks a report failure kind"
            )
        result[failure_kind] = result.get(failure_kind, 0) + 1
    return {key: count for key, count in result.items() if count}


def _validate_provider_delta(
    manifest: Wave4Manifest,
    report: ProviderReliabilityReport,
    baseline: ProviderReliabilityEvidenceCell,
    current: ProviderReliabilityEvidenceCell,
    provider: str,
    cases: Sequence[Wave4ManifestCase],
    baseline_ids: Collection[str],
    wave4_ids: Collection[str],
    supplemental_ids: Collection[str],
    cell_ids: Collection[str],
) -> None:
    profile = f"{provider}-native-executor-read-only"
    if baseline.sample_size and (
        baseline.oldest_evidence_timestamp is None
        or baseline.oldest_evidence_timestamp < report.policy.window_start_at
    ):
        raise ValueError(
            f"Wave 3 baseline evidence for {profile} is not wholly inside the Wave 4 window"
        )
    supplemental = [
        item for item in manifest.supplemental_observations if item.provider == provider
    ]
    provider_cases = [case for case in cases if case.exclusion_reason is None]
    if (
        len(baseline_ids) != baseline.sample_size
        or len(wave4_ids) != len(provider_cases)
        or len(supplemental_ids) != len(supplemental)
    ):
        raise ValueError(f"provenance task counts do not match evidence cells for {profile}")
    expected_ids = set(baseline_ids) | set(wave4_ids) | set(supplemental_ids)
    actual_ids = set(cell_ids)
    missing, extra = len(expected_ids - actual_ids), len(actual_ids - expected_ids)
    if missing or extra:
        raise ValueError(
            "canonical extractor task set does not equal the Wave 3 baseline, eligible "
            "Wave 4 cases, and documented prior attempts "
            f"for {profile} (missing={missing}, extra={extra})"
        )
    sample_size = baseline.sample_size + len(provider_cases) + len(supplemental)
    accepted = baseline.accepted_count + sum(case.accepted for case in provider_cases)
    accepted += sum(item.accepted for item in supplemental)
    failures = baseline.failure_count + sum(not case.accepted for case in provider_cases)
    failures += sum(not item.accepted for item in supplemental)
    typed_failures = _expected_typed_failures(baseline, provider_cases, supplemental, profile)
    if (
        current.sample_size != sample_size
        or current.accepted_count != accepted
        or current.failure_count != failures
        or current.typed_failures != typed_failures
    ):
        raise ValueError(
            f"canonical report counts for {profile} do not equal the Wave 3 baseline "
            "plus manifest-bound Wave 4 and supplemental observations"
        )


def assert_wave4_manifest_matches_report(
    manifest: Wave4Manifest,
    report: ProviderReliabilityReport,
    *,
    baseline_report: ProviderReliabilityReport | None = None,
    extractor_cells: Sequence[ProviderReliabilityEvidenceCell] | None = None,
    baseline_task_ids_by_provider: Mapping[str, Collection[str]] | None = None,
    cell_extractor_task_ids_by_provider: Mapping[str, Collection[str]] | None = None,
    supplemental_task_ids_by_provider: Mapping[str, Collection[str]] | None = None,
    wave4_task_ids_by_provider: Mapping[str, Collection[str]] | None = None,
    extractor_task_ids: Collection[str] | None = None,
) -> None:
    """Require exact Wave 3 + Wave 4 provenance for both cumulative provider cells."""
    _report_policy(manifest, report)
    report_cells = _cell_map(report.evidence_cells)
    extractor_map = _cell_map(extractor_cells) if extractor_cells is not None else {}
    if baseline_report is not None:
        _baseline_policy(manifest, baseline_report)
        if extractor_cells is None or any(
            value is None
            for value in (
                baseline_task_ids_by_provider,
                cell_extractor_task_ids_by_provider,
                supplemental_task_ids_by_provider,
                wave4_task_ids_by_provider,
                extractor_task_ids,
            )
        ):
            raise ValueError("Wave 4 publication requires exact task-set reconciliation")
        assert baseline_task_ids_by_provider is not None
        assert cell_extractor_task_ids_by_provider is not None
        assert supplemental_task_ids_by_provider is not None
        assert wave4_task_ids_by_provider is not None
        assert extractor_task_ids is not None
        _validate_exact_task_sets(
            baseline_task_ids_by_provider,
            cell_extractor_task_ids_by_provider,
            supplemental_task_ids_by_provider,
            wave4_task_ids_by_provider,
            extractor_task_ids,
        )
    recommendations = {
        (item.task_class, item.mutation_mode): item
        for item in getattr(report, "recommendations", [])
    }
    for provider in _PROVIDERS:
        profile = f"{provider}-native-executor-read-only"
        cases = [case for case in manifest.cases if case.provider == provider]
        cell = report_cells.get(("investigation", profile, "read_only"))
        _validate_current_cell(cell, report, profile, cases, recommendations)
        if baseline_report is not None:
            _validate_provider_delta(
                manifest,
                report,
                _baseline_cell(baseline_report, profile),
                cell,
                provider,
                cases,
                baseline_task_ids_by_provider[provider],
                wave4_task_ids_by_provider[provider],
                supplemental_task_ids_by_provider[provider],
                cell_extractor_task_ids_by_provider[provider],
            )
        if extractor_cells is not None:
            _validate_extractor_cell(cell, extractor_map, profile)


def _validate_current_cell(
    cell: ProviderReliabilityEvidenceCell | None,
    report: ProviderReliabilityReport,
    profile: str,
    cases: Sequence[Wave4ManifestCase],
    recommendations: Mapping[tuple[str, str], TaskClassRecommendation],
) -> None:
    if cell is None:
        raise ValueError(f"canonical report is missing investigation/{profile}")
    if cell.sample_size < report.policy.min_samples:
        raise ValueError(
            f"Wave 4 remains unqualified: investigation/{profile} has {cell.sample_size} "
            f"samples, below the policy minimum of {report.policy.min_samples}"
        )
    recommendation = recommendations.get(("investigation", "read_only"))
    if recommendation is None or (
        recommendation.recommended_profile is None and not recommendation.fallback_reason
    ):
        raise ValueError("Wave 4 remains unqualified: investigation/read_only has no decision")
    if any(not case.identity_matches for case in cases if case.exclusion_reason is None):
        raise ValueError("Wave 4 includes a case without a verified matching identity")
    included = sum(case.exclusion_reason is None for case in cases)
    if cell.sample_size < included:
        raise ValueError("canonical report cell is smaller than Wave 4 contribution")


def _validate_extractor_cell(
    cell: ProviderReliabilityEvidenceCell,
    extractor_map: Mapping[tuple[str, str, str], ProviderReliabilityEvidenceCell],
    profile: str,
) -> None:
    key = ("investigation", profile, "read_only")
    extractor_cell = extractor_map.get(key)
    if extractor_cell is None:
        raise ValueError(f"canonical extractor is missing investigation/{profile}")
    if cell != extractor_cell:
        raise ValueError(f"canonical report differs from extractor snapshot for {profile}")
