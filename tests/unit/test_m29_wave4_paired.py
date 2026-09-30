"""Unit coverage for the sanitized Wave 4 investigation evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evaluation.m29_wave4_paired import (
    Wave4Manifest,
    Wave4ManifestCase,
    Wave4SupplementalObservation,
    analyze_wave4_manifest,
    assert_sanitized_wave4_manifest,
    build_wave4_paired_report,
    render_wave4_markdown,
)
from evaluation.m29_wave4_reconciliation import assert_wave4_manifest_matches_report
from evaluation.provider_reliability_models import (
    M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES,
    BudgetCoverageMetrics,
    InterventionMetrics,
    LatencyMetrics,
    ProviderReliabilityEvidenceCell,
    ReliabilityReportPolicy,
    RepairMetrics,
    StageOutcomeRates,
    WilsonConfidenceInterval,
)


def _evidence_cell(provider: str, *, sample_size: int = 16) -> ProviderReliabilityEvidenceCell:
    accepted_count = 5
    return ProviderReliabilityEvidenceCell(
        task_class="investigation",
        profile=f"{provider}-native-executor-read-only",
        mutation_mode="read_only",
        sample_size=sample_size,
        accepted_count=accepted_count,
        accepted_task_rate=round(accepted_count / sample_size, 4),
        accepted_task_rate_ci=WilsonConfidenceInterval(lower=0.1, center=0.3, upper=0.5),
        failure_count=sample_size - accepted_count,
        failure_rate=round((sample_size - accepted_count) / sample_size, 4),
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
        terminal_latency=LatencyMetrics(median_seconds=100.0),
        successful_task_latency=LatencyMetrics(median_seconds=100.0),
        failure_task_latency=LatencyMetrics(median_seconds=100.0),
        budget_field_coverage=BudgetCoverageMetrics(
            budget_reported_count=sample_size,
            budget_coverage_rate=1.0,
        ),
        is_eligible=True,
    )


def _manifest(*, identity_complete: bool = True) -> Wave4Manifest:
    cases: list[Wave4ManifestCase] = []
    for index in range(10):
        pair_group = f"m29-w4-investigation-{index:02d}"
        first_provider = "codex" if index % 2 == 0 else "antigravity"
        ordered = (first_provider, "antigravity" if first_provider == "codex" else "codex")
        for pair_order, provider in enumerate(ordered, start=1):
            accepted = index < 5
            complete = identity_complete or index < 5
            cases.append(
                Wave4ManifestCase(
                    case_id=f"{pair_group}-{provider}",
                    pair_group=pair_group,
                    pair_order=pair_order,
                    task_class="investigation",
                    worker_profile=f"{provider}-native-executor-read-only",
                    provider=provider,
                    terminal_status="completed" if accepted else "failed",
                    accepted=accepted,
                    failure_kind=None if accepted else "worker_failure",
                    report_failure_kind=None if accepted else "worker_failure",
                    time_to_terminal_seconds=100.0 if provider == "codex" else 50.0,
                    execution_identity_status="verified" if complete else "unknown_legacy",
                    identity_matches=complete,
                    exclusion_reason=None if complete else "unknown_execution_identity",
                )
            )
    return Wave4Manifest(
        suite_name="m29-live-provider-evidence-wave4",
        suite_sha256="a" * 64,
        build_sha="b" * 40,
        target_repository_revision="c" * 40,
        as_of=datetime(2026, 9, 20, tzinfo=UTC),
        baseline_report_sha256="9" * 64,
        advisory_report_sha256="d" * 64,
        operational_report_sha256="e" * 64,
        robustness_report_sha256="f" * 64,
        cases=cases,
    )


def test_manifest_distribution_and_bootstrap_are_deterministic() -> None:
    manifest = _manifest()
    first = analyze_wave4_manifest(manifest, iterations=100)
    second = analyze_wave4_manifest(manifest, iterations=100)

    assert first.task_class == "investigation"
    assert first.pair_count == 10
    assert first.identity_complete_pair_count == 10
    assert first.successful_latency_differences.median_seconds == -50.0
    assert first.bootstrap == second.bootstrap


def test_manifest_rejects_duplicate_cases_and_provider_order_bias() -> None:
    manifest = _manifest()
    duplicate = manifest.cases[-1].model_copy(update={"case_id": manifest.cases[0].case_id})
    with pytest.raises(ValueError, match="duplicate case IDs"):
        Wave4Manifest.model_validate(
            manifest.model_copy(update={"cases": [*manifest.cases[:-1], duplicate]}).model_dump()
        )

    biased = [case.model_copy(deep=True) for case in manifest.cases]
    for case in biased:
        if case.pair_group == "m29-w4-investigation-01":
            case.pair_order = 1 if case.provider == "codex" else 2
    with pytest.raises(ValueError, match="does not alternate"):
        Wave4Manifest.model_validate(manifest.model_copy(update={"cases": biased}).model_dump())


def test_manifest_sanitization_rejects_private_fields() -> None:
    payload = _manifest().model_dump(mode="json")
    payload["cases"][0]["task_id"] = "private"
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        assert_sanitized_wave4_manifest(payload)


def test_manifest_matches_cumulative_report_cells() -> None:
    manifest = _manifest()
    cells = [
        SimpleNamespace(
            task_class="investigation",
            profile=f"{provider}-native-executor-read-only",
            mutation_mode="read_only",
            sample_size=15,
            accepted_count=5,
        )
        for provider in ("codex", "antigravity")
    ]
    policy = ReliabilityReportPolicy(
        as_of=manifest.as_of,
        window_start_at=manifest.as_of - timedelta(days=90),
        window_end_at=manifest.as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES),
    )
    recommendation = SimpleNamespace(
        task_class="investigation",
        mutation_mode="read_only",
        recommended_profile="codex-native-executor-read-only",
    )
    assert_wave4_manifest_matches_report(
        manifest,
        SimpleNamespace(policy=policy, evidence_cells=cells, recommendations=[recommendation]),
    )

    smaller = cells[0]
    smaller.sample_size = 9
    with pytest.raises(ValueError, match="remains unqualified"):
        assert_wave4_manifest_matches_report(
            manifest,
            SimpleNamespace(policy=policy, evidence_cells=cells, recommendations=[recommendation]),
        )


def _reconciliation_cell(
    as_of: datetime, provider: str, sample_size: int, accepted_count: int, failures: dict[str, int]
):
    return SimpleNamespace(
        task_class="investigation",
        profile=f"{provider}-native-executor-read-only",
        mutation_mode="read_only",
        sample_size=sample_size,
        accepted_count=accepted_count,
        failure_count=sample_size - accepted_count,
        typed_failures=failures,
        oldest_evidence_timestamp=as_of - timedelta(days=10),
    )


def _reconciliation_manifest() -> Wave4Manifest:
    manifest = _manifest()
    supplemental = Wave4SupplementalObservation(
        case_id="m29-w4-investigation-00-codex",
        task_class="investigation",
        pair_group="m29-w4-investigation-00",
        provider="codex",
        worker_profile="codex-native-executor-read-only",
        terminal_status="failed",
        accepted=False,
        failure_kind="infra_verifier_unavailable",
        report_failure_kind="infra_verifier_unavailable",
        time_to_terminal_seconds=887.2,
        execution_identity_status="verified",
        identity_matches=True,
    )
    return manifest.model_copy(update={"supplemental_observations": [supplemental]})


def _reconciliation_reports(manifest: Wave4Manifest, add_extra: bool):
    current_policy = ReliabilityReportPolicy(
        as_of=manifest.as_of,
        window_start_at=manifest.as_of - timedelta(days=90),
        window_end_at=manifest.as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES),
    )
    baseline_policy = ReliabilityReportPolicy(
        as_of=manifest.as_of - timedelta(days=1),
        window_start_at=manifest.as_of - timedelta(days=91),
        window_end_at=manifest.as_of - timedelta(days=1),
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES),
    )
    baseline_cells = [
        _reconciliation_cell(
            manifest.as_of, "codex", 6, 3, {"infra_verifier_unavailable": 2, "worker_failure": 1}
        ),
        _reconciliation_cell(
            manifest.as_of,
            "antigravity",
            6,
            3,
            {"infra_verifier_unavailable": 2, "worker_failure": 1},
        ),
    ]
    codex_failures = 7 if add_extra else 6
    report_cells = [
        _reconciliation_cell(
            manifest.as_of,
            "codex",
            18 if add_extra else 17,
            8,
            {"infra_verifier_unavailable": 3, "worker_failure": codex_failures},
        ),
        _reconciliation_cell(
            manifest.as_of,
            "antigravity",
            16,
            8,
            {"infra_verifier_unavailable": 2, "worker_failure": 6},
        ),
    ]
    recommendation = SimpleNamespace(
        task_class="investigation",
        mutation_mode="read_only",
        recommended_profile="antigravity-native-executor-read-only",
        fallback_reason=None,
    )
    report = SimpleNamespace(
        policy=current_policy,
        evidence_cells=report_cells,
        recommendations=[recommendation],
    )
    baseline_report = SimpleNamespace(policy=baseline_policy, evidence_cells=baseline_cells)
    return report, baseline_report


def _reconciliation_task_sets(add_extra: bool):
    baseline_ids = {
        provider: {f"{provider}-base-{i}" for i in range(6)}
        for provider in ("codex", "antigravity")
    }
    wave4_ids = {
        provider: {f"{provider}-wave4-{i}" for i in range(10)}
        for provider in ("codex", "antigravity")
    }
    supplemental_ids = {"codex": {"codex-prior-wave4"}, "antigravity": set()}
    cell_ids = {
        provider: baseline_ids[provider] | wave4_ids[provider] | supplemental_ids[provider]
        for provider in ("codex", "antigravity")
    }
    if add_extra:
        cell_ids["codex"].add("unmanifested-observation")
    return baseline_ids, wave4_ids, supplemental_ids, cell_ids, set().union(*cell_ids.values())


def _exact_reconciliation_inputs(*, add_unmanifested_task: bool = False):
    manifest = _reconciliation_manifest()
    report, baseline_report = _reconciliation_reports(manifest, add_unmanifested_task)
    task_sets = _reconciliation_task_sets(add_unmanifested_task)
    return (manifest, report, baseline_report, *task_sets)


def test_manifest_reconciles_exact_baseline_wave4_and_supplemental_delta() -> None:
    (
        manifest,
        report,
        baseline_report,
        baseline_ids,
        wave4_ids,
        supplemental_ids,
        cell_ids,
        all_task_ids,
    ) = _exact_reconciliation_inputs()
    assert_wave4_manifest_matches_report(
        manifest,
        report,
        baseline_report=baseline_report,
        extractor_cells=report.evidence_cells,
        baseline_task_ids_by_provider=baseline_ids,
        cell_extractor_task_ids_by_provider=cell_ids,
        supplemental_task_ids_by_provider=supplemental_ids,
        wave4_task_ids_by_provider=wave4_ids,
        extractor_task_ids=all_task_ids,
    )


def test_manifest_rejects_an_unmanifested_current_cohort_observation() -> None:
    (
        manifest,
        report,
        baseline_report,
        baseline_ids,
        wave4_ids,
        supplemental_ids,
        cell_ids,
        all_task_ids,
    ) = _exact_reconciliation_inputs(add_unmanifested_task=True)
    with pytest.raises(ValueError, match="task set does not equal"):
        assert_wave4_manifest_matches_report(
            manifest,
            report,
            baseline_report=baseline_report,
            extractor_cells=report.evidence_cells,
            baseline_task_ids_by_provider=baseline_ids,
            cell_extractor_task_ids_by_provider=cell_ids,
            supplemental_task_ids_by_provider=supplemental_ids,
            wave4_task_ids_by_provider=wave4_ids,
            extractor_task_ids=all_task_ids,
        )


def test_paired_markdown_distinguishes_all_pairs_from_identity_complete_pairs() -> None:
    original = _manifest()
    cases = []
    for case in original.cases:
        index = int(case.pair_group.rsplit("-", maxsplit=1)[1])
        accepted = case.provider == "antigravity" or index < 6
        identity_complete = index < 7
        cases.append(
            case.model_copy(
                update={
                    "accepted": accepted,
                    "terminal_status": "completed" if accepted else "failed",
                    "failure_kind": None if accepted else "worker_failure",
                    "report_failure_kind": None if accepted else "worker_failure",
                    "execution_identity_status": "verified"
                    if identity_complete
                    else "unknown_legacy",
                    "identity_matches": identity_complete,
                    "exclusion_reason": None if identity_complete else "unknown_execution_identity",
                }
            )
        )
    codex_case = next(case for case in cases if case.provider == "codex")
    supplemental = Wave4SupplementalObservation(
        case_id=codex_case.case_id,
        task_class="investigation",
        pair_group=codex_case.pair_group,
        provider="codex",
        worker_profile=codex_case.worker_profile,
        terminal_status="failed",
        accepted=False,
        failure_kind="infra_verifier_unavailable",
        report_failure_kind="infra_verifier_unavailable",
        time_to_terminal_seconds=887.2,
        execution_identity_status="verified",
        identity_matches=True,
    )
    manifest = Wave4Manifest.model_validate(
        {
            **original.model_dump(mode="python"),
            "cases": cases,
            "supplemental_observations": [supplemental],
        }
    )
    report = build_wave4_paired_report(manifest, manifest_sha256="0" * 64, iterations=20)

    markdown = render_wave4_markdown(report, manifest)

    assert "Across the 10 frozen pairs" in markdown
    assert (
        "Among the 7 identity-complete pairs, 6 had both providers complete, 1 was Antigravity-only"
        in markdown
    )
    assert "1 earlier identity-verified attempt included in the cumulative report" in markdown


def test_excluded_identity_cases_do_not_enter_paired_bootstrap() -> None:
    analysis = analyze_wave4_manifest(_manifest(identity_complete=False), iterations=100)
    assert analysis.identity_complete_pair_count == 5
    assert analysis.bootstrap.identity_complete_pairs == 5


def test_all_failure_cohort_is_publishable_with_explicit_fallback() -> None:
    manifest = _manifest()
    failures = [
        case.model_copy(
            update={
                "accepted": False,
                "terminal_status": "failed",
                "failure_kind": "worker_failure",
            }
        )
        for case in manifest.cases
    ]
    manifest = manifest.model_copy(update={"cases": failures})
    policy = ReliabilityReportPolicy(
        as_of=manifest.as_of,
        window_start_at=manifest.as_of - timedelta(days=90),
        window_end_at=manifest.as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES),
    )
    cells = [
        SimpleNamespace(
            task_class="investigation",
            profile=f"{provider}-native-executor-read-only",
            mutation_mode="read_only",
            sample_size=10,
            accepted_count=0,
        )
        for provider in ("codex", "antigravity")
    ]
    fallback = SimpleNamespace(
        task_class="investigation",
        mutation_mode="read_only",
        recommended_profile=None,
        fallback_reason="no_successful_candidates",
    )

    assert_wave4_manifest_matches_report(
        manifest,
        SimpleNamespace(policy=policy, evidence_cells=cells, recommendations=[fallback]),
    )


def test_out_of_window_case_is_excluded_from_cohort_pair_statistics() -> None:
    manifest = _manifest().model_copy(deep=True)
    manifest.cases[0] = manifest.cases[0].model_copy(update={"exclusion_reason": "outside_window"})

    analysis = analyze_wave4_manifest(manifest, iterations=100)

    assert analysis.identity_complete_pair_count == 9
    assert analysis.bootstrap.identity_complete_pairs == 9
    assert analysis.successful_latency_differences.sample_size == 4


def test_underpowered_excluded_wave4_cases_remain_unqualified() -> None:
    manifest = _manifest(identity_complete=False)
    policy = ReliabilityReportPolicy(
        as_of=manifest.as_of,
        window_start_at=manifest.as_of - timedelta(days=90),
        window_end_at=manifest.as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES),
    )
    cells = [
        SimpleNamespace(
            task_class="investigation",
            profile=f"{provider}-native-executor-read-only",
            mutation_mode="read_only",
            sample_size=9,
            accepted_count=4,
        )
        for provider in ("codex", "antigravity")
    ]
    with pytest.raises(ValueError, match="remains unqualified"):
        assert_wave4_manifest_matches_report(
            manifest,
            SimpleNamespace(policy=policy, evidence_cells=cells, recommendations=[]),
        )


def test_wave4_manifest_reconciles_against_frozen_baseline() -> None:
    (
        manifest,
        report,
        baseline,
        baseline_ids,
        wave4_ids,
        supplemental_ids,
        cell_ids,
        all_task_ids,
    ) = _exact_reconciliation_inputs()
    extractor_cells = list(report.evidence_cells)
    extractor_cells[0] = SimpleNamespace(**vars(report.evidence_cells[0]))
    extractor_cells[0].sample_size -= 1
    with pytest.raises(ValueError, match="differs from extractor snapshot"):
        assert_wave4_manifest_matches_report(
            manifest,
            report,
            baseline_report=baseline,
            extractor_cells=extractor_cells,
            baseline_task_ids_by_provider=baseline_ids,
            cell_extractor_task_ids_by_provider=cell_ids,
            supplemental_task_ids_by_provider=supplemental_ids,
            wave4_task_ids_by_provider=wave4_ids,
            extractor_task_ids=all_task_ids,
        )
