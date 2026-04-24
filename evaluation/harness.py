"""Core data models and deterministic scoring for frozen-suite evaluations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class ReviewExpectation:
    """Optional reviewer-quality expectations for one frozen evaluation case."""

    expected_outcome: Literal["no_findings", "findings"] | None = None
    expect_fix_after_review: bool | None = None


@dataclass(frozen=True, slots=True)
class TaskExpectation:
    """Expected output constraints for one frozen evaluation case."""

    require_success: bool = True
    require_tests_passed: bool = False
    required_files_changed: tuple[str, ...] = ()
    required_summary_substrings: tuple[str, ...] = ()
    review: ReviewExpectation | None = None


@dataclass(frozen=True, slots=True)
class FrozenTaskCase:
    """One deterministic task input from the frozen benchmark suite."""

    case_id: str
    repo_fixture: str
    task_text: str
    expectation: TaskExpectation


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """Normalized execution output used by the local evaluation harness."""

    status: Literal["success", "failure", "error"]
    summary: str
    files_changed: tuple[str, ...] = ()
    tests_passed: bool | None = None
    review: ReviewOutcome | None = None


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """Optional normalized reviewer-quality outcome data for one case."""

    findings_count: int = 0
    actionable_findings_count: int = 0
    false_positive_findings_count: int = 0
    fix_after_review_attempted: bool | None = None
    fix_after_review_succeeded: bool | None = None


@dataclass(frozen=True, slots=True)
class ReviewMetrics:
    """Aggregate reviewer quality metrics for one report."""

    reviewed_cases: int
    precision: float | None
    actionable_rate: float | None
    false_positive_rate: float | None
    fix_after_review_success: float | None
    empty_review_correctness: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewed_cases": self.reviewed_cases,
            "precision": self.precision,
            "actionable_rate": self.actionable_rate,
            "false_positive_rate": self.false_positive_rate,
            "fix_after_review_success": self.fix_after_review_success,
            "empty_review_correctness": self.empty_review_correctness,
        }


@dataclass(frozen=True, slots=True)
class EvaluationProfile:
    """Optional run metadata to support A/B comparisons across reviewer variants."""

    variant_label: str | None = None
    review_prompt_profile: str | None = None
    reviewer_model_profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_label": self.variant_label,
            "review_prompt_profile": self.review_prompt_profile,
            "reviewer_model_profile": self.reviewer_model_profile,
        }


@dataclass(frozen=True, slots=True)
class EvaluationComparison:
    """Structured comparison between two evaluation report variants."""

    baseline_variant_label: str | None
    candidate_variant_label: str | None
    delta_passed_cases: int
    delta_total_score: int
    delta_precision: float | None
    delta_actionable_rate: float | None
    delta_false_positive_rate: float | None
    delta_fix_after_review_success: float | None
    delta_empty_review_correctness: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_variant_label": self.baseline_variant_label,
            "candidate_variant_label": self.candidate_variant_label,
            "delta_passed_cases": self.delta_passed_cases,
            "delta_total_score": self.delta_total_score,
            "delta_precision": self.delta_precision,
            "delta_actionable_rate": self.delta_actionable_rate,
            "delta_false_positive_rate": self.delta_false_positive_rate,
            "delta_fix_after_review_success": self.delta_fix_after_review_success,
            "delta_empty_review_correctness": self.delta_empty_review_correctness,
        }


class EvaluationRunner(Protocol):
    """Runner boundary for orchestrator/replay adapters used by the harness."""

    async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
        """Execute one case and return the normalized outcome."""


def _normalize_path_for_scoring(raw_path: str) -> str:
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
            f"evaluation runner raised {type(exc).__name__} for case " f"'{case.case_id}': {detail}"
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
        }


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

    changed_files = {_normalize_path_for_scoring(path) for path in outcome.files_changed}
    for required_file in case.expectation.required_files_changed:
        if _normalize_path_for_scoring(required_file) in changed_files:
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
    )


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


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
    reviewed_results = [
        result
        for result in results
        if result.outcome.review is not None or result.case_id in expectations_by_case_id
    ]
    if not reviewed_results:
        return ReviewMetrics(
            reviewed_cases=0,
            precision=None,
            actionable_rate=None,
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

    fix_expected_cases = [
        result
        for result in reviewed_results
        if expectations_by_case_id.get(
            result.case_id,
            ReviewExpectation(),
        ).expect_fix_after_review
    ]
    fix_successes = sum(
        1
        for result in fix_expected_cases
        if result.outcome.review is not None
        and result.outcome.review.fix_after_review_succeeded is True
    )

    empty_expected_cases = [
        result
        for result in reviewed_results
        if expectations_by_case_id.get(result.case_id, ReviewExpectation()).expected_outcome
        == "no_findings"
    ]
    empty_correct_cases = sum(
        1
        for result in empty_expected_cases
        if result.outcome.review is not None and result.outcome.review.findings_count == 0
    )

    return ReviewMetrics(
        reviewed_cases=len(reviewed_results),
        precision=_safe_ratio(actionable_findings, total_findings),
        actionable_rate=_safe_ratio(actionable_cases, len(reviewed_results)),
        false_positive_rate=_safe_ratio(false_positive_findings, total_findings),
        fix_after_review_success=_safe_ratio(fix_successes, len(fix_expected_cases)),
        empty_review_correctness=_safe_ratio(empty_correct_cases, len(empty_expected_cases)),
    )


def _delta_metric(candidate: float | None, baseline: float | None) -> float | None:
    if candidate is None or baseline is None:
        return None
    return candidate - baseline


_REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD: dict[str, str] = {
    "precision": "delta_precision",
    "actionable_rate": "delta_actionable_rate",
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

    payload: dict[str, float | None] = {}
    for metric_field_name, delta_field_name in _REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD.items():
        baseline_value = (
            getattr(baseline_metrics, metric_field_name) if baseline_metrics is not None else None
        )
        candidate_value = (
            getattr(candidate_metrics, metric_field_name) if candidate_metrics is not None else None
        )
        payload[delta_field_name] = _delta_metric(candidate_value, baseline_value)
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
    return EvaluationComparison(
        baseline_variant_label=(
            baseline.profile.variant_label if baseline.profile is not None else None
        ),
        candidate_variant_label=(
            candidate.profile.variant_label if candidate.profile is not None else None
        ),
        delta_passed_cases=candidate.passed_cases - baseline.passed_cases,
        delta_total_score=candidate.total_score - baseline.total_score,
        delta_precision=delta_payload["delta_precision"],
        delta_actionable_rate=delta_payload["delta_actionable_rate"],
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
    )


def write_report(report: EvaluationReport, output_path: Path) -> None:
    """Persist a deterministic JSON report for local and CI diffing."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report.to_dict(), file, indent=2, sort_keys=True)
        file.write("\n")
