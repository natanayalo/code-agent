"""Core data models and deterministic scoring for frozen-suite evaluations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from evaluation.models import (
    EvaluationComparison,
    EvaluationProfile,
    FrozenTaskCase,
    ReliabilityMetrics,
    ReliabilityReport,
    ReviewExpectation,
    ReviewMetrics,
    WorkerOutcome,
)

# ---------------------------------------------------------------------------
# M20.0 Reliability Metrics
# ---------------------------------------------------------------------------


class EvaluationRunner(Protocol):
    """Runner boundary for orchestrator/replay adapters used by the harness."""

    async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
        """Execute one case and return the normalized outcome."""
        ...


def normalize_path_for_scoring(raw_path: str) -> str:
    """Normalize file paths for robust cross-environment comparison."""
    normalized = PurePosixPath(raw_path.replace("\\", "/")).as_posix()
    if normalized.startswith("./"):
        return normalized[2:]
    return normalized


def _runner_exception_outcome(case: FrozenTaskCase, exc: Exception) -> WorkerOutcome:
    """Normalize unexpected runner crashes into deterministic failure outcomes."""
    detail = str(exc).strip()
    if detail:
        summary = (
            f"evaluation runner raised {type(exc).__name__} for case '{case.case_id}': {detail}"
        )
    else:
        summary = f"evaluation runner raised {type(exc).__name__} for case '{case.case_id}'"
    return WorkerOutcome(status="error", summary=summary, tests_passed=False)


class ReplayRunner:
    """Deterministic adapter that replays pre-baked outcomes by case id."""

    def __init__(self, outcomes_by_case_id: dict[str, WorkerOutcome]) -> None:
        self._outcomes_by_case_id = dict(outcomes_by_case_id)

    async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
        outcome = self._outcomes_by_case_id.get(case.case_id)
        if outcome is not None:
            return outcome
        return WorkerOutcome(
            status="failure",
            summary=f"Missing replay outcome for case '{case.case_id}'.",
        )


@dataclass(frozen=True, slots=True)
class CaseRunResult:
    """Scored evaluation result for one case."""

    case_id: str
    passed: bool
    score: int
    max_score: int
    failures: tuple[str, ...]
    outcome: WorkerOutcome
    reliability: ReliabilityMetrics | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "score": self.score,
            "max_score": self.max_score,
            "failures": list(self.failures),
            "outcome": {
                "status": self.outcome.status,
                "summary": self.outcome.summary,
                "files_changed": list(self.outcome.files_changed),
                "tests_passed": self.outcome.tests_passed,
                "review": (
                    None
                    if self.outcome.review is None
                    else {
                        "findings_count": self.outcome.review.findings_count,
                        "actionable_findings_count": (
                            self.outcome.review.actionable_findings_count
                        ),
                        "false_positive_findings_count": (
                            self.outcome.review.false_positive_findings_count
                        ),
                        "fix_after_review_attempted": (
                            self.outcome.review.fix_after_review_attempted
                        ),
                        "fix_after_review_succeeded": (
                            self.outcome.review.fix_after_review_succeeded
                        ),
                    }
                ),
            },
            "reliability": (None if self.reliability is None else self.reliability.to_dict()),
        }


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Top-level deterministic report for one frozen-suite run."""

    suite_name: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    total_score: int
    max_score: int
    results: tuple[CaseRunResult, ...]
    review_metrics: ReviewMetrics | None = None
    profile: EvaluationProfile | None = None
    comparison: EvaluationComparison | None = None
    reliability_report: ReliabilityReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_name": self.suite_name,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "total_score": self.total_score,
            "max_score": self.max_score,
            "results": [result.to_dict() for result in self.results],
            "review_metrics": (
                None if self.review_metrics is None else self.review_metrics.to_dict()
            ),
            "profile": None if self.profile is None else self.profile.to_dict(),
            "comparison": None if self.comparison is None else self.comparison.to_dict(),
            "reliability_report": (
                None if self.reliability_report is None else self.reliability_report.to_dict()
            ),
        }


def _safe_mean(values: list[int]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _compute_reliability_report(
    *,
    results: tuple[CaseRunResult, ...],
) -> ReliabilityReport:
    """Aggregate per-case ReliabilityMetrics into a suite-level report."""
    reliability_results = [
        result.reliability for result in results if result.reliability is not None
    ]
    total_cases = len(results)

    if not reliability_results:
        return ReliabilityReport(
            total_cases=total_cases,
            cases_needing_approval=0,
            cases_with_validation_evidence=0,
            cases_needing_manual_log_inspection=0,
            cases_with_worker_failure=0,
            worker_failure_kind_counts=(),
            mean_commands_run=None,
            mean_files_changed=None,
            mean_friction_reports=None,
            stage_latency_available=False,
            mean_stage_latency_seconds=(),
        )

    failure_kind_tally: dict[str, int] = {}
    stage_latency_sums: dict[str, float] = {}
    stage_latency_counts: dict[str, int] = {}
    any_latency_available = False

    for rm in reliability_results:
        if rm.worker_failure_kind:
            failure_kind_tally[rm.worker_failure_kind] = (
                failure_kind_tally.get(rm.worker_failure_kind, 0) + 1
            )
        if rm.stage_latency_available:
            any_latency_available = True
            for stage, elapsed in rm.stage_latency_seconds:
                stage_latency_sums[stage] = stage_latency_sums.get(stage, 0.0) + elapsed
                stage_latency_counts[stage] = stage_latency_counts.get(stage, 0) + 1

    mean_stage: list[tuple[str, float]] = [
        (stage, stage_latency_sums[stage] / stage_latency_counts[stage])
        for stage in sorted(stage_latency_sums)
    ]

    return ReliabilityReport(
        total_cases=total_cases,
        cases_needing_approval=sum(1 for rm in reliability_results if rm.approval_required),
        cases_with_validation_evidence=sum(
            1 for rm in reliability_results if rm.validation_evidence_present
        ),
        cases_needing_manual_log_inspection=sum(
            1 for rm in reliability_results if rm.manual_log_inspection_needed
        ),
        cases_with_worker_failure=sum(
            1
            for rm in reliability_results
            if rm.worker_status is not None and rm.worker_status != "success"
        ),
        worker_failure_kind_counts=tuple(sorted(failure_kind_tally.items())),
        mean_commands_run=_safe_mean([rm.commands_run_count for rm in reliability_results]),
        mean_files_changed=_safe_mean([rm.files_changed_count for rm in reliability_results]),
        mean_friction_reports=_safe_mean([rm.friction_report_count for rm in reliability_results]),
        stage_latency_available=any_latency_available,
        mean_stage_latency_seconds=tuple(mean_stage),
    )


def _score_case(case: FrozenTaskCase, outcome: WorkerOutcome) -> CaseRunResult:
    points = 0
    max_points = (
        int(case.expectation.require_success)
        + int(case.expectation.require_tests_passed)
        + len(case.expectation.required_files_changed)
        + len(case.expectation.required_summary_substrings)
    )
    failures: list[str] = []

    if case.expectation.require_success:
        if outcome.status == "success":
            points += 1
        else:
            failures.append("status was not success")

    if case.expectation.require_tests_passed:
        if outcome.tests_passed is True:
            points += 1
        else:
            failures.append("tests were expected to pass")

    changed_files = {normalize_path_for_scoring(path) for path in outcome.files_changed}
    for required_file in case.expectation.required_files_changed:
        if normalize_path_for_scoring(required_file) in changed_files:
            points += 1
        else:
            failures.append(f"required file was not changed: {required_file}")

    summary_text = outcome.summary.lower()
    for required_substring in case.expectation.required_summary_substrings:
        if required_substring.lower() in summary_text:
            points += 1
        else:
            failures.append(f"required summary substring missing: {required_substring}")

    return CaseRunResult(
        case_id=case.case_id,
        passed=points == max_points,
        score=points,
        max_score=max_points,
        failures=tuple(failures),
        outcome=outcome,
        reliability=outcome.reliability,
    )


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _reviewed_results(
    *,
    cases: tuple[FrozenTaskCase, ...],
    results: tuple[CaseRunResult, ...],
) -> tuple[CaseRunResult, ...]:
    expectations_by_case_id = {
        case.case_id: case.expectation.review
        for case in cases
        if case.expectation.review is not None
    }
    return tuple(
        result
        for result in results
        if result.outcome.review is not None or result.case_id in expectations_by_case_id
    )


def _expected_fix_cases(
    *,
    reviewed_results: tuple[CaseRunResult, ...],
    expectations_by_case_id: dict[str, ReviewExpectation],
) -> list[CaseRunResult]:
    return [
        result
        for result in reviewed_results
        if expectations_by_case_id.get(result.case_id, ReviewExpectation()).expect_fix_after_review
    ]


def _expected_empty_cases(
    *,
    reviewed_results: tuple[CaseRunResult, ...],
    expectations_by_case_id: dict[str, ReviewExpectation],
) -> list[CaseRunResult]:
    return [
        result
        for result in reviewed_results
        if expectations_by_case_id.get(result.case_id, ReviewExpectation()).expected_outcome
        == "no_findings"
    ]


def _compute_review_metrics(
    *,
    cases: tuple[FrozenTaskCase, ...],
    results: tuple[CaseRunResult, ...],
) -> ReviewMetrics:
    expectations_by_case_id = {
        case.case_id: case.expectation.review
        for case in cases
        if case.expectation.review is not None
    }
    reviewed_results = _reviewed_results(cases=cases, results=results)
    if not reviewed_results:
        return ReviewMetrics(
            reviewed_cases=0,
            precision=None,
            actionable_rate=None,
            false_discovery_rate=None,
            false_positive_rate=None,
            fix_after_review_success=None,
            empty_review_correctness=None,
        )

    total_findings = sum(
        0 if result.outcome.review is None else result.outcome.review.findings_count
        for result in reviewed_results
    )
    actionable_findings = sum(
        0 if result.outcome.review is None else result.outcome.review.actionable_findings_count
        for result in reviewed_results
    )
    false_positive_findings = sum(
        0 if result.outcome.review is None else result.outcome.review.false_positive_findings_count
        for result in reviewed_results
    )

    actionable_cases = sum(
        1
        for result in reviewed_results
        if result.outcome.review is not None and result.outcome.review.actionable_findings_count > 0
    )

    fix_expected_cases = _expected_fix_cases(
        reviewed_results=reviewed_results,
        expectations_by_case_id=expectations_by_case_id,
    )
    fix_successes = sum(
        1
        for result in fix_expected_cases
        if result.outcome.review is not None
        and result.outcome.review.fix_after_review_succeeded is True
    )

    empty_expected_cases = _expected_empty_cases(
        reviewed_results=reviewed_results,
        expectations_by_case_id=expectations_by_case_id,
    )
    empty_correct_cases = sum(
        1
        for result in empty_expected_cases
        if result.outcome.review is not None and result.outcome.review.findings_count == 0
    )

    return ReviewMetrics(
        reviewed_cases=len(reviewed_results),
        precision=_safe_ratio(actionable_findings, total_findings),
        actionable_rate=_safe_ratio(actionable_cases, len(reviewed_results)),
        false_discovery_rate=_safe_ratio(false_positive_findings, total_findings),
        # Backward-compatible alias for existing consumers.
        false_positive_rate=_safe_ratio(false_positive_findings, total_findings),
        fix_after_review_success=_safe_ratio(fix_successes, len(fix_expected_cases)),
        empty_review_correctness=_safe_ratio(empty_correct_cases, len(empty_expected_cases)),
    )


def _delta_metric(
    candidate: float | None,
    baseline: float | None,
    *,
    treat_missing_as_zero: bool = False,
) -> float | None:
    if treat_missing_as_zero:
        candidate_value = 0.0 if candidate is None else candidate
        baseline_value = 0.0 if baseline is None else baseline
        return candidate_value - baseline_value
    if candidate is None or baseline is None:
        return None
    return candidate - baseline


_REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD: dict[str, str] = {
    "precision": "delta_precision",
    "actionable_rate": "delta_actionable_rate",
    "false_discovery_rate": "delta_false_discovery_rate",
    "false_positive_rate": "delta_false_positive_rate",
    "fix_after_review_success": "delta_fix_after_review_success",
    "empty_review_correctness": "delta_empty_review_correctness",
}


def _metric_delta_payload(
    *,
    baseline_metrics: ReviewMetrics | None,
    candidate_metrics: ReviewMetrics | None,
) -> dict[str, float | None]:
    review_metric_field_names = {
        field_info.name
        for field_info in dataclass_fields(ReviewMetrics)
        if field_info.name != "reviewed_cases"
    }
    mapped_field_names = set(_REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD)
    if review_metric_field_names != mapped_field_names:
        missing = sorted(review_metric_field_names - mapped_field_names)
        extra = sorted(mapped_field_names - review_metric_field_names)
        details: list[str] = []
        if missing:
            details.append(f"missing mappings for {', '.join(missing)}")
        if extra:
            details.append(f"unexpected mappings for {', '.join(extra)}")
        detail_text = "; ".join(details) if details else "unknown mapping mismatch"
        raise ValueError(f"Review metric delta mapping is out of sync: {detail_text}")
    comparison_metric_delta_field_names = {
        field_info.name
        for field_info in dataclass_fields(EvaluationComparison)
        if field_info.name.startswith("delta_")
        and field_info.name
        not in {"delta_passed_cases", "delta_total_score", "delta_reviewed_cases"}
    }
    mapped_delta_field_names = set(_REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD.values())
    if comparison_metric_delta_field_names != mapped_delta_field_names:
        missing = sorted(comparison_metric_delta_field_names - mapped_delta_field_names)
        extra = sorted(mapped_delta_field_names - comparison_metric_delta_field_names)
        details: list[str] = []
        if missing:
            details.append(f"missing mappings for {', '.join(missing)}")
        if extra:
            details.append(f"unexpected mappings for {', '.join(extra)}")
        detail_text = "; ".join(details) if details else "unknown comparison mismatch"
        raise ValueError(f"Comparison delta field mapping is out of sync: {detail_text}")

    payload: dict[str, float | None] = {}
    for metric_field_name, delta_field_name in _REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD.items():
        baseline_value = (
            getattr(baseline_metrics, metric_field_name) if baseline_metrics is not None else None
        )
        candidate_value = (
            getattr(candidate_metrics, metric_field_name) if candidate_metrics is not None else None
        )
        payload[delta_field_name] = _delta_metric(
            candidate_value,
            baseline_value,
            # Keep delta signals visible for silent->active reviewer comparisons.
            # Use actionable_rate and delta_reviewed_cases alongside precision to interpret volume.
            treat_missing_as_zero=(
                metric_field_name
                in {
                    "precision",
                    "false_discovery_rate",
                    "false_positive_rate",
                    "fix_after_review_success",
                    "empty_review_correctness",
                }
            ),
        )
    return payload


def compare_reports(
    *,
    baseline: EvaluationReport,
    candidate: EvaluationReport,
) -> EvaluationComparison:
    """Compute structured A/B deltas between two evaluation reports."""
    baseline_metrics = baseline.review_metrics
    candidate_metrics = candidate.review_metrics
    delta_payload = _metric_delta_payload(
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
    )
    baseline_reviewed_cases = baseline_metrics.reviewed_cases if baseline_metrics is not None else 0
    candidate_reviewed_cases = (
        candidate_metrics.reviewed_cases if candidate_metrics is not None else 0
    )
    return EvaluationComparison(
        baseline_variant_label=(
            baseline.profile.variant_label if baseline.profile is not None else None
        ),
        candidate_variant_label=(
            candidate.profile.variant_label if candidate.profile is not None else None
        ),
        delta_passed_cases=candidate.passed_cases - baseline.passed_cases,
        delta_total_score=candidate.total_score - baseline.total_score,
        delta_reviewed_cases=candidate_reviewed_cases - baseline_reviewed_cases,
        delta_precision=delta_payload["delta_precision"],
        delta_actionable_rate=delta_payload["delta_actionable_rate"],
        delta_false_discovery_rate=delta_payload["delta_false_discovery_rate"],
        delta_false_positive_rate=delta_payload["delta_false_positive_rate"],
        delta_fix_after_review_success=delta_payload["delta_fix_after_review_success"],
        delta_empty_review_correctness=delta_payload["delta_empty_review_correctness"],
    )


async def evaluate_suite(
    *,
    suite_name: str,
    cases: tuple[FrozenTaskCase, ...],
    runner: EvaluationRunner,
    parallel: bool = False,
    max_parallel_cases: int | None = None,
    profile: EvaluationProfile | None = None,
) -> EvaluationReport:
    """Execute and score all frozen cases through the supplied runner."""

    async def _execute_case(case: FrozenTaskCase) -> CaseRunResult:
        try:
            outcome = await runner.run_case(case)
        except Exception as exc:
            outcome = _runner_exception_outcome(case, exc)
        return _score_case(case, outcome)

    if parallel:
        if max_parallel_cases is not None:
            if max_parallel_cases < 1:
                raise ValueError("max_parallel_cases must be at least 1 when provided")
            semaphore = asyncio.Semaphore(max_parallel_cases)

            async def _execute_case_with_limit(case: FrozenTaskCase) -> CaseRunResult:
                async with semaphore:
                    return await _execute_case(case)

            scored_results = await asyncio.gather(
                *(_execute_case_with_limit(case) for case in cases)
            )
        else:
            scored_results = await asyncio.gather(*(_execute_case(case) for case in cases))
    else:
        scored_results: list[CaseRunResult] = []
        for case in cases:
            scored_results.append(await _execute_case(case))

    results = tuple(scored_results)
    total_cases = len(results)
    passed_cases = sum(1 for result in results if result.passed)
    failed_cases = total_cases - passed_cases
    total_score = sum(result.score for result in results)
    max_score = sum(result.max_score for result in results)
    review_metrics = _compute_review_metrics(cases=cases, results=results)
    reliability_report = _compute_reliability_report(results=results)

    return EvaluationReport(
        suite_name=suite_name,
        total_cases=total_cases,
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        total_score=total_score,
        max_score=max_score,
        results=results,
        review_metrics=review_metrics,
        profile=profile,
        reliability_report=reliability_report,
    )


def write_report(report: EvaluationReport, output_path: Path) -> None:
    """Persist a deterministic JSON report for local and CI diffing."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report.to_dict(), file, indent=2, sort_keys=True)
        file.write("\n")
