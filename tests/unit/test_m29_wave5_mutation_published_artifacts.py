"""Unit tests for committed M29 Wave 5 feature/mutation published artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evaluation.m29_mutation_manifest import (
    M29MutationManifest,
    M29MutationPairedAnalysisReport,
    assert_sanitized_mutation_manifest,
    assert_sanitized_mutation_paired_report,
    manifest_sha256,
    render_mutation_paired_markdown,
)
from evaluation.m29_mutation_runner_archive import reconcile_m29_publication_gate
from evaluation.m29_mutation_suite import build_m29_mutation_suite
from evaluation.provider_reliability_models import (
    BudgetCoverageMetrics,
    InterventionMetrics,
    LatencyMetrics,
    ProviderReliabilityEvidenceCell,
    ProviderReliabilityReport,
    ProviderReliabilityRobustnessReport,
    RepairMetrics,
    StageOutcomeRates,
    WilsonConfidenceInterval,
)
from evaluation.provider_reliability_report import assert_sanitized_report

EVAL_DIR = Path(__file__).resolve().parents[2] / "evaluation"


def _load_manifest() -> M29MutationManifest:
    manifest_path = EVAL_DIR / "m29_provider_reliability_mutation_manifest.json"
    return M29MutationManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def _load_paired_report() -> M29MutationPairedAnalysisReport:
    paired_path = EVAL_DIR / "m29_provider_reliability_mutation_paired_analysis.json"
    return M29MutationPairedAnalysisReport.model_validate_json(
        paired_path.read_text(encoding="utf-8")
    )


def _make_baseline_codex_cell() -> ProviderReliabilityEvidenceCell:
    return ProviderReliabilityEvidenceCell(
        task_class="feature",
        profile="codex-native-executor",
        mutation_mode="mutation",
        sample_size=21,
        accepted_count=20,
        accepted_task_rate=20 / 21,
        accepted_task_rate_ci=WilsonConfidenceInterval(lower=0.7638, center=0.9022, upper=0.9889),
        failure_count=1,
        failure_rate=1 / 21,
        typed_failures={"worker_failure": 1},
        stage_outcome_rates=StageOutcomeRates(
            dispatch_rate=1.0, execution_success_rate=0.9524, verification_pass_rate=0.0
        ),
        repairs=RepairMetrics(
            verifier_repairs_count=1,
            review_repairs_count=0,
            total_repaired_tasks=1,
            repair_rate=0.0476,
        ),
        interventions=InterventionMetrics(
            human_interventions_count=0,
            clarification_questions_count=0,
            approvals_count=0,
            intervention_rate=0.0,
        ),
        manual_overrides_count=0,
        manual_override_rate=0.0,
        terminal_latency=LatencyMetrics(median_seconds=26.53),
        successful_task_latency=LatencyMetrics(median_seconds=26.49),
        failure_task_latency=LatencyMetrics(median_seconds=37.12),
        budget_field_coverage=BudgetCoverageMetrics(
            budget_reported_count=21, budget_coverage_rate=1.0
        ),
        is_eligible=True,
    )


def _make_baseline_ag_cell() -> ProviderReliabilityEvidenceCell:
    return ProviderReliabilityEvidenceCell(
        task_class="feature",
        profile="antigravity-native-executor",
        mutation_mode="mutation",
        sample_size=16,
        accepted_count=14,
        accepted_task_rate=14 / 16,
        accepted_task_rate_ci=WilsonConfidenceInterval(lower=0.6272, center=0.8122, upper=0.9678),
        failure_count=2,
        failure_rate=2 / 16,
        typed_failures={"tool_runtime": 2},
        stage_outcome_rates=StageOutcomeRates(
            dispatch_rate=1.0, execution_success_rate=0.875, verification_pass_rate=None
        ),
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
        terminal_latency=LatencyMetrics(median_seconds=184.45),
        successful_task_latency=LatencyMetrics(median_seconds=172.93),
        failure_task_latency=LatencyMetrics(median_seconds=306.18),
        budget_field_coverage=BudgetCoverageMetrics(
            budget_reported_count=16, budget_coverage_rate=1.0
        ),
        is_eligible=True,
    )


def _load_baseline_report(canonical_report: ProviderReliabilityReport) -> ProviderReliabilityReport:
    b_cells = [_make_baseline_codex_cell(), _make_baseline_ag_cell()]
    return ProviderReliabilityReport(
        schema_version=canonical_report.schema_version,
        generated_at=canonical_report.generated_at,
        status="partial",
        policy=canonical_report.policy,
        profile_coverage=canonical_report.profile_coverage,
        exclusions=canonical_report.exclusions,
        evidence_cells=b_cells,
        recommendations=[],
    )


def test_published_mutation_manifest_sanitized() -> None:
    manifest_path = EVAL_DIR / "m29_provider_reliability_mutation_manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert_sanitized_mutation_manifest(manifest_data)

    manifest = M29MutationManifest.model_validate(manifest_data)
    assert manifest.schema_version == 1
    assert manifest.suite_name == "m29-live-provider-evidence-mutation"
    assert len(manifest.cases) == 20

    codex_cases = [c for c in manifest.cases if c.provider == "codex"]
    ag_cases = [c for c in manifest.cases if c.provider == "antigravity"]
    assert len(codex_cases) == 10
    assert len(ag_cases) == 10
    assert all(c.accepted for c in codex_cases)
    assert sum(c.accepted for c in ag_cases) == 9


def test_published_mutation_suite_digest_binding() -> None:
    manifest = _load_manifest()
    suite = build_m29_mutation_suite()
    assert manifest.suite_sha256 == suite.compute_digest()
    assert manifest.target_repository_revision == suite.target_repository_revision


def test_published_reports_sha256_digests() -> None:
    manifest = _load_manifest()

    can_bytes = (EVAL_DIR / "m29_provider_reliability_report.json").read_bytes()
    op_bytes = (EVAL_DIR / "m29_provider_reliability_operational_report.json").read_bytes()
    rob_bytes = (EVAL_DIR / "m29_provider_reliability_robustness_report.json").read_bytes()

    assert hashlib.sha256(can_bytes).hexdigest() == manifest.advisory_report_sha256
    assert hashlib.sha256(op_bytes).hexdigest() == manifest.operational_report_sha256
    assert hashlib.sha256(rob_bytes).hexdigest() == manifest.robustness_report_sha256


def test_published_reports_sanitized() -> None:
    can_text = (EVAL_DIR / "m29_provider_reliability_report.json").read_text(encoding="utf-8")
    op_text = (EVAL_DIR / "m29_provider_reliability_operational_report.json").read_text(
        encoding="utf-8"
    )
    assert_sanitized_report(can_text)
    assert_sanitized_report(op_text)


def test_published_mutation_paired_analysis() -> None:
    manifest = _load_manifest()
    paired_report = _load_paired_report()

    paired_data = json.loads(
        (EVAL_DIR / "m29_provider_reliability_mutation_paired_analysis.json").read_text(
            encoding="utf-8"
        )
    )
    assert_sanitized_mutation_paired_report(paired_data)

    computed_manifest_sha = manifest_sha256(manifest)
    assert paired_report.manifest_sha256 == computed_manifest_sha
    assert paired_report.paired_summary.pair_count == 10
    assert paired_report.paired_summary.both_completed_count == 9
    assert paired_report.paired_summary.codex_only_completed_count == 1
    assert paired_report.paired_summary.antigravity_only_completed_count == 0
    assert paired_report.paired_summary.both_quality_pass_count == 8
    assert paired_report.paired_summary.codex_only_quality_pass_count == 2
    assert paired_report.paired_summary.antigravity_only_quality_pass_count == 0


def test_published_mutation_paired_markdown_rendering() -> None:
    paired_report = _load_paired_report()
    rendered_md = render_mutation_paired_markdown(paired_report).strip()
    disk_md = (
        (EVAL_DIR / "m29_provider_reliability_mutation_paired_analysis.md")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert rendered_md == disk_md


def test_reconcile_m29_publication_gate_passes() -> None:
    manifest = _load_manifest()
    suite = build_m29_mutation_suite()

    can_json = (EVAL_DIR / "m29_provider_reliability_report.json").read_text(encoding="utf-8")
    can_report = ProviderReliabilityReport.model_validate_json(can_json)
    op_json = (EVAL_DIR / "m29_provider_reliability_operational_report.json").read_text(
        encoding="utf-8"
    )
    rob_json = (EVAL_DIR / "m29_provider_reliability_robustness_report.json").read_text(
        encoding="utf-8"
    )
    rob_report = ProviderReliabilityRobustnessReport.model_validate_json(rob_json)

    baseline_report = _load_baseline_report(can_report)

    assert reconcile_m29_publication_gate(
        manifest=manifest,
        suite=suite,
        canonical_report=can_report,
        robustness_report=rob_report,
        baseline_report=baseline_report,
        canonical_report_json=can_json,
        robustness_report_json=rob_json,
        operational_report_json=op_json,
    )
