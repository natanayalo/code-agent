"""Unit coverage for the sanitized Wave 4 investigation evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evaluation.m29_wave4_paired import (
    Wave4Manifest,
    Wave4ManifestCase,
    analyze_wave4_manifest,
    assert_sanitized_wave4_manifest,
    assert_wave4_manifest_matches_report,
)
from evaluation.provider_reliability_models import ReliabilityReportPolicy


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


def test_excluded_identity_cases_do_not_enter_paired_bootstrap() -> None:
    analysis = analyze_wave4_manifest(_manifest(identity_complete=False), iterations=100)
    assert analysis.identity_complete_pair_count == 5
    assert analysis.bootstrap.identity_complete_pairs == 5


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
    manifest = _manifest()
    policy = ReliabilityReportPolicy(
        as_of=manifest.as_of,
        window_start_at=manifest.as_of - timedelta(days=90),
        window_end_at=manifest.as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
    )
    recommendation = SimpleNamespace(
        task_class="investigation",
        mutation_mode="read_only",
        recommended_profile="codex-native-executor-read-only",
    )
    current_cells = [
        SimpleNamespace(
            task_class="investigation",
            profile=f"{provider}-native-executor-read-only",
            mutation_mode="read_only",
            sample_size=15,
            accepted_count=5,
        )
        for provider in ("codex", "antigravity")
    ]
    baseline_cells = [
        SimpleNamespace(
            task_class="investigation",
            profile=f"{provider}-native-executor-read-only",
            mutation_mode="read_only",
            sample_size=5,
            accepted_count=0,
        )
        for provider in ("codex", "antigravity")
    ]
    current = SimpleNamespace(
        policy=policy, evidence_cells=current_cells, recommendations=[recommendation]
    )
    baseline = SimpleNamespace(
        policy=policy.model_copy(update={"as_of": manifest.as_of - timedelta(days=1)}),
        evidence_cells=baseline_cells,
    )
    assert_wave4_manifest_matches_report(manifest, current, baseline_report=baseline)

    current_cells[0].sample_size += 1
    with pytest.raises(ValueError, match="contribution mismatch"):
        assert_wave4_manifest_matches_report(manifest, current, baseline_report=baseline)
