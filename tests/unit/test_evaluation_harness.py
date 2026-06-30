"""Unit tests for the deterministic frozen evaluation harness (T-106 slice)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evaluation import (
    ReliabilityMetrics,
    ReliabilityReport,
    ReplayRunner,
    ReviewExpectation,
    ReviewOutcome,
    TaskExpectation,
    WorkerOutcome,
    compare_reports,
    default_replay_outcomes,
    evaluate_suite,
    load_frozen_suite,
    write_report,
)
from evaluation.harness import (
    _REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD,
    EvaluationComparison,
    FrozenTaskCase,
    ReviewMetrics,
)


def test_evaluate_suite_is_deterministic_for_same_inputs() -> None:
    suite = load_frozen_suite()
    replay_runner = ReplayRunner(default_replay_outcomes(suite.cases))

    report_one = asyncio.run(
        evaluate_suite(
            suite_name=suite.suite_name,
            cases=suite.cases,
            runner=replay_runner,
        )
    )
    report_two = asyncio.run(
        evaluate_suite(
            suite_name=suite.suite_name,
            cases=suite.cases,
            runner=ReplayRunner(default_replay_outcomes(suite.cases)),
        )
    )

    assert report_one.to_dict() == report_two.to_dict()


def test_evaluate_suite_continues_after_runner_exception() -> None:
    class CrashyRunner:
        def __init__(self) -> None:
            self._seen: set[str] = set()

        async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
            if case.case_id == "boom" and case.case_id not in self._seen:
                self._seen.add(case.case_id)
                raise RuntimeError("transient crash")
            return WorkerOutcome(status="success", summary="ok")

    cases = (
        FrozenTaskCase(
            case_id="boom",
            repo_fixture="fixtures/a",
            task_text="task a",
            expectation=TaskExpectation(require_success=True),
        ),
        FrozenTaskCase(
            case_id="ok",
            repo_fixture="fixtures/b",
            task_text="task b",
            expectation=TaskExpectation(require_success=True),
        ),
    )

    report = asyncio.run(
        evaluate_suite(
            suite_name="crashy",
            cases=cases,
            runner=CrashyRunner(),
        )
    )

    assert report.total_cases == 2
    assert report.failed_cases == 1
    assert report.results[0].outcome.status == "error"
    assert "evaluation runner raised runtimeerror" in report.results[0].outcome.summary.lower()
    assert report.results[1].outcome.status == "success"


def test_evaluate_suite_parallel_mode_preserves_input_order() -> None:
    class StaggeredRunner:
        async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
            delays = {"first": 0.03, "second": 0.0, "third": 0.01}
            await asyncio.sleep(delays[case.case_id])
            return WorkerOutcome(status="success", summary=f"done {case.case_id}")

    cases = (
        FrozenTaskCase(
            case_id="first",
            repo_fixture="fixtures/a",
            task_text="task first",
            expectation=TaskExpectation(require_success=True),
        ),
        FrozenTaskCase(
            case_id="second",
            repo_fixture="fixtures/b",
            task_text="task second",
            expectation=TaskExpectation(require_success=True),
        ),
        FrozenTaskCase(
            case_id="third",
            repo_fixture="fixtures/c",
            task_text="task third",
            expectation=TaskExpectation(require_success=True),
        ),
    )

    report = asyncio.run(
        evaluate_suite(
            suite_name="parallel-order",
            cases=cases,
            runner=StaggeredRunner(),
            parallel=True,
        )
    )

    assert tuple(result.case_id for result in report.results) == ("first", "second", "third")
    assert all(result.passed for result in report.results)


def test_evaluate_suite_parallel_mode_normalizes_exceptions_deterministically() -> None:
    class CrashyParallelRunner:
        async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
            if case.case_id == "boom":
                await asyncio.sleep(0.0)
                raise RuntimeError("parallel crash")
            await asyncio.sleep(0.01)
            return WorkerOutcome(status="success", summary=f"ok {case.case_id}")

    cases = (
        FrozenTaskCase(
            case_id="boom",
            repo_fixture="fixtures/a",
            task_text="task a",
            expectation=TaskExpectation(require_success=True),
        ),
        FrozenTaskCase(
            case_id="ok",
            repo_fixture="fixtures/b",
            task_text="task b",
            expectation=TaskExpectation(require_success=True),
        ),
    )

    report = asyncio.run(
        evaluate_suite(
            suite_name="parallel-crashy",
            cases=cases,
            runner=CrashyParallelRunner(),
            parallel=True,
        )
    )

    assert report.total_cases == 2
    assert report.failed_cases == 1
    assert report.results[0].case_id == "boom"
    assert report.results[0].outcome.status == "error"
    assert "evaluation runner raised runtimeerror" in report.results[0].outcome.summary.lower()
    assert report.results[1].case_id == "ok"
    assert report.results[1].outcome.status == "success"


def test_evaluate_suite_parallel_mode_respects_concurrency_limit() -> None:
    class ConcurrencyTrackingRunner:
        def __init__(self) -> None:
            self._active = 0
            self.max_active = 0
            self._lock = asyncio.Lock()

        async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
            async with self._lock:
                self._active += 1
                if self._active > self.max_active:
                    self.max_active = self._active
            await asyncio.sleep(0.01)
            async with self._lock:
                self._active -= 1
            return WorkerOutcome(status="success", summary=f"ok {case.case_id}")

    runner = ConcurrencyTrackingRunner()
    cases = tuple(
        FrozenTaskCase(
            case_id=f"case-{index}",
            repo_fixture="fixtures/empty",
            task_text="Do a thing",
            expectation=TaskExpectation(require_success=True),
        )
        for index in range(5)
    )

    report = asyncio.run(
        evaluate_suite(
            suite_name="parallel-limit",
            cases=cases,
            runner=runner,
            parallel=True,
            max_parallel_cases=2,
        )
    )

    assert report.passed_cases == 5
    assert runner.max_active == 2


def test_evaluate_suite_rejects_non_positive_parallel_limit() -> None:
    case = FrozenTaskCase(
        case_id="one",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )

    with pytest.raises(ValueError, match="max_parallel_cases must be at least 1"):
        asyncio.run(
            evaluate_suite(
                suite_name="bad-limit",
                cases=(case,),
                runner=ReplayRunner(
                    outcomes_by_case_id={"one": WorkerOutcome(status="success", summary="ok")}
                ),
                parallel=True,
                max_parallel_cases=0,
            )
        )


def test_missing_replay_outcome_is_scored_as_failure() -> None:
    case = FrozenTaskCase(
        case_id="missing-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    report = asyncio.run(
        evaluate_suite(
            suite_name="one-case-suite",
            cases=(case,),
            runner=ReplayRunner(outcomes_by_case_id={}),
        )
    )

    assert report.total_cases == 1
    assert report.failed_cases == 1
    assert report.results[0].outcome.status == "failure"
    assert "missing replay outcome" in report.results[0].outcome.summary.lower()


def test_scoring_asserts_failure_when_success_not_required() -> None:
    case = FrozenTaskCase(
        case_id="no-success-weight",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(
            require_success=False,
            required_summary_substrings=("needle",),
        ),
    )
    report = asyncio.run(
        evaluate_suite(
            suite_name="weighting",
            cases=(case,),
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "no-success-weight": WorkerOutcome(
                        status="failure",
                        summary="contains needle",
                    )
                }
            ),
        )
    )

    assert report.total_score == 2
    assert report.max_score == 2
    assert report.results[0].passed is True


def test_scoring_normalizes_required_and_changed_paths() -> None:
    case = FrozenTaskCase(
        case_id="path-normalization",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(
            require_success=False,
            required_files_changed=("src/app.py",),
        ),
    )
    report = asyncio.run(
        evaluate_suite(
            suite_name="path-normalization",
            cases=(case,),
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "path-normalization": WorkerOutcome(
                        status="success",
                        summary="ok",
                        files_changed=("./src\\app.py",),
                    )
                }
            ),
        )
    )

    assert report.total_score == 1
    assert report.max_score == 2
    assert report.results[0].passed is False


def test_write_report_persists_structured_json(tmp_path: Path) -> None:
    suite = load_frozen_suite()
    report = asyncio.run(
        evaluate_suite(
            suite_name=suite.suite_name,
            cases=suite.cases,
            runner=ReplayRunner(default_replay_outcomes(suite.cases)),
        )
    )
    output_path = tmp_path / "eval-report.json"

    write_report(report, output_path)

    with output_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    assert payload["suite_name"] == suite.suite_name
    assert payload["total_cases"] == len(suite.cases)
    assert payload["passed_cases"] == len(suite.cases)
    assert payload["results"][0]["case_id"] == suite.cases[0].case_id
    assert payload["review_metrics"]["reviewed_cases"] == 0


def test_evaluate_suite_computes_review_quality_metrics() -> None:
    cases = (
        FrozenTaskCase(
            case_id="review-findings",
            repo_fixture="fixtures/a",
            task_text="review case one",
            expectation=TaskExpectation(
                require_success=False,
                review=ReviewExpectation(
                    expected_outcome="findings",
                    expect_fix_after_review=True,
                ),
            ),
        ),
        FrozenTaskCase(
            case_id="review-empty",
            repo_fixture="fixtures/b",
            task_text="review case two",
            expectation=TaskExpectation(
                require_success=False,
                review=ReviewExpectation(expected_outcome="no_findings"),
            ),
        ),
    )

    report = asyncio.run(
        evaluate_suite(
            suite_name="review-metrics",
            cases=cases,
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "review-findings": WorkerOutcome(
                        status="success",
                        summary="reviewed",
                        review=ReviewOutcome(
                            findings_count=2,
                            actionable_findings_count=1,
                            false_positive_findings_count=1,
                            fix_after_review_attempted=True,
                            fix_after_review_succeeded=True,
                        ),
                    ),
                    "review-empty": WorkerOutcome(
                        status="success",
                        summary="empty",
                        review=ReviewOutcome(
                            findings_count=0,
                            actionable_findings_count=0,
                            false_positive_findings_count=0,
                        ),
                    ),
                }
            ),
        )
    )

    assert report.review_metrics is not None
    assert report.review_metrics.reviewed_cases == 2
    assert report.review_metrics.precision == pytest.approx(0.5)
    assert report.review_metrics.actionable_rate == pytest.approx(0.5)
    assert report.review_metrics.false_discovery_rate == pytest.approx(0.5)
    assert report.review_metrics.false_positive_rate == pytest.approx(0.5)
    assert report.review_metrics.fix_after_review_success == pytest.approx(1.0)
    assert report.review_metrics.empty_review_correctness == pytest.approx(1.0)


def test_compare_reports_includes_review_metric_deltas() -> None:
    cases = (
        FrozenTaskCase(
            case_id="case-1",
            repo_fixture="fixtures/a",
            task_text="review case",
            expectation=TaskExpectation(
                require_success=False,
                review=ReviewExpectation(expected_outcome="findings"),
            ),
        ),
    )
    report_baseline = asyncio.run(
        evaluate_suite(
            suite_name="ab-compare",
            cases=cases,
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "case-1": WorkerOutcome(
                        status="success",
                        summary="baseline",
                        review=ReviewOutcome(
                            findings_count=2,
                            actionable_findings_count=1,
                            false_positive_findings_count=1,
                        ),
                    )
                }
            ),
        )
    )
    report_candidate = asyncio.run(
        evaluate_suite(
            suite_name="ab-compare",
            cases=cases,
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "case-1": WorkerOutcome(
                        status="success",
                        summary="candidate",
                        review=ReviewOutcome(
                            findings_count=2,
                            actionable_findings_count=2,
                            false_positive_findings_count=0,
                        ),
                    )
                }
            ),
        )
    )

    comparison = compare_reports(baseline=report_baseline, candidate=report_candidate)

    assert comparison.delta_total_score == 0
    assert comparison.delta_reviewed_cases == 0
    assert comparison.delta_precision == pytest.approx(0.5)
    assert comparison.delta_false_discovery_rate == pytest.approx(-0.5)
    assert comparison.delta_false_positive_rate == pytest.approx(-0.5)


def test_compare_reports_treats_missing_baseline_precision_as_zero() -> None:
    cases = (
        FrozenTaskCase(
            case_id="case-1",
            repo_fixture="fixtures/a",
            task_text="review case",
            expectation=TaskExpectation(require_success=False),
        ),
    )
    report_baseline = asyncio.run(
        evaluate_suite(
            suite_name="ab-precision-none",
            cases=cases,
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "case-1": WorkerOutcome(
                        status="success",
                        summary="baseline",
                        review=ReviewOutcome(
                            findings_count=0,
                            actionable_findings_count=0,
                            false_positive_findings_count=0,
                        ),
                    )
                }
            ),
        )
    )
    report_candidate = asyncio.run(
        evaluate_suite(
            suite_name="ab-precision-none",
            cases=cases,
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "case-1": WorkerOutcome(
                        status="success",
                        summary="candidate",
                        review=ReviewOutcome(
                            findings_count=1,
                            actionable_findings_count=1,
                            false_positive_findings_count=0,
                        ),
                    )
                }
            ),
        )
    )

    comparison = compare_reports(baseline=report_baseline, candidate=report_candidate)

    assert comparison.delta_precision == pytest.approx(1.0)
    assert comparison.delta_false_discovery_rate == pytest.approx(0.0)
    assert comparison.delta_false_positive_rate == pytest.approx(0.0)


def test_compare_reports_treats_missing_fix_and_empty_metrics_as_zero() -> None:
    baseline_cases = (
        FrozenTaskCase(
            case_id="case-1",
            repo_fixture="fixtures/a",
            task_text="review case",
            expectation=TaskExpectation(require_success=False),
        ),
    )
    candidate_cases = (
        FrozenTaskCase(
            case_id="case-1",
            repo_fixture="fixtures/a",
            task_text="review case",
            expectation=TaskExpectation(
                require_success=False,
                review=ReviewExpectation(
                    expect_fix_after_review=True,
                    expected_outcome="no_findings",
                ),
            ),
        ),
    )
    report_baseline = asyncio.run(
        evaluate_suite(
            suite_name="ab-missing-fix-empty",
            cases=baseline_cases,
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "case-1": WorkerOutcome(
                        status="success",
                        summary="baseline",
                        review=ReviewOutcome(
                            findings_count=0,
                            actionable_findings_count=0,
                            false_positive_findings_count=0,
                            fix_after_review_succeeded=True,
                        ),
                    )
                }
            ),
        )
    )
    report_candidate = asyncio.run(
        evaluate_suite(
            suite_name="ab-missing-fix-empty",
            cases=candidate_cases,
            runner=ReplayRunner(
                outcomes_by_case_id={
                    "case-1": WorkerOutcome(
                        status="success",
                        summary="candidate",
                        review=ReviewOutcome(
                            findings_count=0,
                            actionable_findings_count=0,
                            false_positive_findings_count=0,
                            fix_after_review_succeeded=True,
                        ),
                    )
                }
            ),
        )
    )

    comparison = compare_reports(baseline=report_baseline, candidate=report_candidate)

    assert comparison.delta_fix_after_review_success == pytest.approx(1.0)
    assert comparison.delta_empty_review_correctness == pytest.approx(1.0)


def test_compare_reports_delta_mapping_covers_all_review_metrics() -> None:
    review_metric_fields = {
        field_name
        for field_name in ReviewMetrics.__dataclass_fields__
        if field_name != "reviewed_cases"
    }
    mapped_metric_fields = set(_REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD)
    comparison_delta_fields = {
        field_name
        for field_name in EvaluationComparison.__dataclass_fields__
        if field_name.startswith("delta_")
        and field_name
        not in {
            "delta_passed_cases",
            "delta_total_score",
            "delta_reviewed_cases",
            "delta_cases_with_validation_evidence",
            "delta_cases_needing_approval",
            "delta_cases_needing_manual_log_inspection",
            "delta_cases_with_worker_failure",
            "delta_mean_commands_run",
            "delta_mean_files_changed",
            "delta_mean_friction_reports",
            "delta_repair_loops_total",
            "delta_mean_time_to_pr_seconds",
            "delta_ci_rejection_total",
            "delta_review_rejection_total",
        }
    }
    mapped_comparison_delta_fields = set(_REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD.values())

    assert mapped_metric_fields == review_metric_fields
    assert mapped_comparison_delta_fields == comparison_delta_fields


# ---------------------------------------------------------------------------
# M20.0 ReliabilityMetrics and ReliabilityReport tests
# ---------------------------------------------------------------------------


def test_reliability_metrics_to_dict_serializes_all_fields() -> None:
    metrics = ReliabilityMetrics(
        human_interaction_count=2,
        repeated_question_count=1,
        validation_evidence_present=True,
        manual_log_inspection_needed=False,
        worker_status="success",
        worker_failure_kind=None,
        next_action_hint="summarize_result",
        friction_report_count=0,
        files_changed_count=3,
        commands_run_count=5,
        test_results_count=4,
        approval_required=False,
        approval_status="not_required",
        stage_latency_seconds=(),
        stage_latency_available=False,
        attempt_count=1,
    )

    d = metrics.to_dict()

    assert d["human_interaction_count"] == 2
    assert d["repeated_question_count"] == 1
    assert d["validation_evidence_present"] is True
    assert d["manual_log_inspection_needed"] is False
    assert d["worker_status"] == "success"
    assert d["worker_failure_kind"] is None
    assert d["next_action_hint"] == "summarize_result"
    assert d["friction_report_count"] == 0
    assert d["files_changed_count"] == 3
    assert d["commands_run_count"] == 5
    assert d["test_results_count"] == 4
    assert d["approval_required"] is False
    assert d["approval_status"] == "not_required"
    assert d["stage_latency_seconds"] == {}
    assert d["stage_latency_available"] is False
    assert d["attempt_count"] == 1


def test_reliability_metrics_stage_latency_dict() -> None:
    metrics = ReliabilityMetrics(
        stage_latency_seconds=(("dispatch_job", 1.5), ("verify_result", 2.3)),
        stage_latency_available=True,
    )

    d = metrics.stage_latency_dict()

    assert d == {"dispatch_job": pytest.approx(1.5), "verify_result": pytest.approx(2.3)}


def test_reliability_report_to_dict_serializes_all_fields() -> None:
    report = ReliabilityReport(
        total_cases=3,
        cases_needing_approval=1,
        cases_with_validation_evidence=2,
        cases_needing_manual_log_inspection=1,
        cases_with_worker_failure=1,
        worker_failure_kind_counts=(("unknown", 1),),
        mean_commands_run=4.0,
        mean_files_changed=2.0,
        mean_friction_reports=0.5,
        repair_loops_total=0,
        mean_time_to_pr_seconds=None,
        ci_rejection_total=0,
        review_rejection_total=0,
        validation_failure_category_counts=(),
        worker_profile_success_rates=(),
        provider_failure_cause_counts=(),
        stage_latency_available=False,
        mean_stage_latency_seconds=(),
    )

    d = report.to_dict()

    assert d["total_cases"] == 3
    assert d["cases_needing_approval"] == 1
    assert d["cases_with_validation_evidence"] == 2
    assert d["cases_needing_manual_log_inspection"] == 1
    assert d["cases_with_worker_failure"] == 1
    assert d["worker_failure_kind_counts"] == {"unknown": 1}
    assert d["mean_commands_run"] == pytest.approx(4.0)
    assert d["mean_files_changed"] == pytest.approx(2.0)
    assert d["mean_friction_reports"] == pytest.approx(0.5)
    assert d["stage_latency_available"] is False
    assert d["mean_stage_latency_seconds"] == {}


def test_evaluate_suite_populates_reliability_report_from_runner_with_reliability() -> None:
    """ReliabilityReport should aggregate when outcomes carry ReliabilityMetrics."""
    metrics_pass = ReliabilityMetrics(
        worker_status="success",
        validation_evidence_present=True,
        manual_log_inspection_needed=False,
        approval_required=False,
        commands_run_count=3,
        files_changed_count=2,
    )
    metrics_fail = ReliabilityMetrics(
        worker_status="failure",
        worker_failure_kind="test",
        validation_evidence_present=False,
        manual_log_inspection_needed=True,
        approval_required=True,
        approval_status="pending",
        commands_run_count=1,
        files_changed_count=0,
    )

    cases = (
        FrozenTaskCase(
            case_id="rel-pass",
            repo_fixture="fixtures/a",
            task_text="Pass case",
            expectation=TaskExpectation(require_success=True),
        ),
        FrozenTaskCase(
            case_id="rel-fail",
            repo_fixture="fixtures/b",
            task_text="Fail case",
            expectation=TaskExpectation(require_success=False),
        ),
    )

    class _ReliabilityRunner:
        async def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
            if case.case_id == "rel-pass":
                return WorkerOutcome(status="success", summary="ok", reliability=metrics_pass)
            return WorkerOutcome(status="failure", summary="fail", reliability=metrics_fail)

    report = asyncio.run(
        evaluate_suite(
            suite_name="reliability-suite",
            cases=cases,
            runner=_ReliabilityRunner(),
        )
    )

    rr = report.reliability_report
    assert rr is not None
    assert rr.total_cases == 2
    assert rr.cases_needing_approval == 1
    assert rr.cases_with_validation_evidence == 1
    assert rr.cases_needing_manual_log_inspection == 1
    assert rr.cases_with_worker_failure == 1
    assert rr.worker_failure_kind_counts_dict() == {"test": 1}
    assert rr.mean_commands_run == pytest.approx(2.0)
    assert rr.mean_files_changed == pytest.approx(1.0)
    assert rr.stage_latency_available is False


def test_evaluate_suite_reliability_report_empty_when_no_reliability_fields() -> None:
    """ReliabilityReport gracefully handles outcomes with no reliability attached."""
    case = FrozenTaskCase(
        case_id="no-rel",
        repo_fixture="fixtures/a",
        task_text="A thing",
        expectation=TaskExpectation(require_success=True),
    )

    report = asyncio.run(
        evaluate_suite(
            suite_name="no-reliability",
            cases=(case,),
            runner=ReplayRunner(
                outcomes_by_case_id={"no-rel": WorkerOutcome(status="success", summary="ok")}
            ),
        )
    )

    rr = report.reliability_report
    assert rr is not None
    assert rr.total_cases == 1
    assert rr.cases_needing_approval == 0
    assert rr.mean_commands_run is None
