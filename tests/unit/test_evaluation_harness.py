"""Unit tests for the deterministic frozen evaluation harness (T-106 slice)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evaluation import (
    OrchestratorReplayRunner,
    ReplayRunner,
    ReviewExpectation,
    ReviewOutcome,
    TaskExpectation,
    WorkerOutcome,
    compare_reports,
    default_replay_outcomes,
    evaluate_suite,
    load_frozen_suite,
    load_replay_outcomes,
    write_report,
)
from evaluation.harness import (
    _REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD,
    EvaluationComparison,
    FrozenTaskCase,
    ReviewMetrics,
)


def test_frozen_suite_has_minimum_case_count() -> None:
    suite = load_frozen_suite()

    assert suite.suite_name == "frozen-v1"
    assert len(suite.cases) >= 5


def test_loader_accepts_small_targeted_suite_file(tmp_path: Path) -> None:
    payload = {
        "suite_name": "targeted",
        "cases": [
            {
                "case_id": "targeted-1",
                "repo_fixture": "fixtures/one",
                "task_text": "Do one thing",
                "expectation": {"require_success": True},
            }
        ],
    }
    suite_path = tmp_path / "targeted-suite.json"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    suite = load_frozen_suite(path=suite_path)

    assert suite.suite_name == "targeted"
    assert len(suite.cases) == 1


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


def test_scoring_omits_success_weight_when_success_not_required() -> None:
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

    assert report.total_score == 1
    assert report.max_score == 1
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
    assert report.max_score == 1
    assert report.results[0].passed is True


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


def test_orchestrator_runner_executes_case_through_graph_path() -> None:
    suite = load_frozen_suite()
    case = suite.cases[0]
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id=default_replay_outcomes(suite.cases),
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "success"
    assert "zero division" in outcome.summary
    assert set(case.expectation.required_files_changed).issubset(set(outcome.files_changed))
    assert outcome.tests_passed is True


def test_orchestrator_runner_propagates_review_outcome_fields() -> None:
    case = FrozenTaskCase(
        case_id="reviewed-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"reviewed-case": WorkerOutcome(status="success", summary="ok")},
        worker_override="codex",
    )

    class _FakeGraph:
        async def ainvoke(self, _inputs: object, config: dict[str, object]) -> dict[str, object]:
            assert config["configurable"] == {"thread_id": "frozen-eval-reviewed-case"}
            return {
                "task": {"task_text": "Do a thing"},
                "dispatch": {"worker_type": "codex"},
                "verification": {"status": "passed", "items": []},
                "repair_handoff_requested": True,
                "result": {
                    "status": "success",
                    "summary": "completed",
                    "commands_run": [],
                    "files_changed": ["src/app.py"],
                    "test_results": [{"name": "suite", "status": "passed", "details": "ok"}],
                    "artifacts": [],
                },
                "review": {
                    "reviewer_kind": "independent_reviewer",
                    "summary": "one issue surfaced",
                    "confidence": 0.8,
                    "outcome": "findings",
                    "findings": [
                        {
                            "severity": "high",
                            "category": "logic",
                            "confidence": 0.9,
                            "file_path": "src/app.py",
                            "line_start": 12,
                            "line_end": 13,
                            "title": "Missing guard",
                            "why_it_matters": "Can crash on empty input.",
                        }
                    ],
                    "suppressed_findings": [
                        {
                            "finding": {
                                "severity": "low",
                                "category": "style",
                                "confidence": 0.6,
                                "file_path": "src/app.py",
                                "line_start": 2,
                                "title": "Minor formatting",
                                "why_it_matters": "Consistency",
                            },
                            "reasons": ["style category suppressed by policy (style)"],
                        }
                    ],
                },
            }

    runner._graph = _FakeGraph()  # type: ignore[assignment]

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.review is not None
    assert outcome.review.findings_count == 2
    assert outcome.review.actionable_findings_count == 1
    assert outcome.review.false_positive_findings_count == 1
    assert outcome.review.fix_after_review_attempted is True
    assert outcome.review.fix_after_review_succeeded is True


def test_orchestrator_runner_review_metrics_precision_reflects_suppressed_findings() -> None:
    case = FrozenTaskCase(
        case_id="review-metrics-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"review-metrics-case": WorkerOutcome(status="success", summary="ok")},
        worker_override="codex",
    )

    class _FakeGraph:
        async def ainvoke(self, _inputs: object, config: dict[str, object]) -> dict[str, object]:
            assert config["configurable"] == {"thread_id": "frozen-eval-review-metrics-case"}
            return {
                "task": {"task_text": "Do a thing"},
                "dispatch": {"worker_type": "codex"},
                "verification": {"status": "passed", "items": []},
                "repair_handoff_requested": False,
                "result": {
                    "status": "success",
                    "summary": "completed",
                    "commands_run": [],
                    "files_changed": ["src/app.py"],
                    "test_results": [{"name": "suite", "status": "passed", "details": "ok"}],
                    "artifacts": [],
                },
                "review": {
                    "reviewer_kind": "independent_reviewer",
                    "summary": "reviewed",
                    "confidence": 0.8,
                    "outcome": "findings",
                    "findings": [
                        {
                            "severity": "high",
                            "category": "logic",
                            "confidence": 0.9,
                            "file_path": "src/app.py",
                            "line_start": 12,
                            "line_end": 13,
                            "title": "Missing guard",
                            "why_it_matters": "Can crash on empty input.",
                        }
                    ],
                    "suppressed_findings": [
                        {
                            "finding": {
                                "severity": "low",
                                "category": "style",
                                "confidence": 0.6,
                                "file_path": "src/app.py",
                                "line_start": 2,
                                "title": "Minor formatting",
                                "why_it_matters": "Consistency",
                            },
                            "reasons": ["style category suppressed by policy (style)"],
                        }
                    ],
                },
            }

    runner._graph = _FakeGraph()  # type: ignore[assignment]
    orchestrator_outcome = asyncio.run(runner.run_case(case))
    assert orchestrator_outcome.review is not None

    class _SingleOutcomeRunner:
        async def run_case(self, _case: FrozenTaskCase) -> WorkerOutcome:
            return orchestrator_outcome

    report = asyncio.run(
        evaluate_suite(
            suite_name="orchestrator-review-metrics",
            cases=(case,),
            runner=_SingleOutcomeRunner(),
        )
    )

    assert report.review_metrics is not None
    assert report.review_metrics.precision == pytest.approx(0.5)
    assert report.review_metrics.false_positive_rate == pytest.approx(0.5)


def test_orchestrator_runner_reports_failure_for_missing_case_outcome() -> None:
    case = FrozenTaskCase(
        case_id="missing-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(outcomes_by_case_id={}, worker_override="codex")

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "failure"
    assert "missing replay outcome" in outcome.summary.lower()


def test_orchestrator_runner_supports_gemini_override() -> None:
    case = FrozenTaskCase(
        case_id="gemini-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={
            "gemini-case": WorkerOutcome(status="success", summary="gemini path ok")
        },
        worker_override="gemini",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "success"
    assert "gemini path ok" in outcome.summary


def test_orchestrator_runner_preserves_error_status() -> None:
    case = FrozenTaskCase(
        case_id="error-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"error-case": WorkerOutcome(status="error", summary="worker crashed")},
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "error"
    assert "worker crashed" in outcome.summary


def test_orchestrator_runner_handles_approval_interrupt_as_failure() -> None:
    case = FrozenTaskCase(
        case_id="destructive-case",
        repo_fixture="fixtures/empty",
        task_text="Please rm -rf temporary files",
        expectation=TaskExpectation(require_success=False),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={
            "destructive-case": WorkerOutcome(status="success", summary="would not run")
        },
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "failure"
    assert "interrupted awaiting approval" in outcome.summary.lower()


def test_load_replay_outcomes_accepts_error_status(tmp_path: Path) -> None:
    replay_path = tmp_path / "replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "case-1": {
                    "status": "error",
                    "summary": "system-level failure",
                    "files_changed": [],
                    "tests_passed": False,
                }
            }
        ),
        encoding="utf-8",
    )

    outcomes = load_replay_outcomes(replay_path)

    assert outcomes["case-1"].status == "error"
    assert outcomes["case-1"].summary == "system-level failure"


def test_load_frozen_suite_accepts_review_expectation_payload(tmp_path: Path) -> None:
    suite_path = tmp_path / "review-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "review-suite",
                "cases": [
                    {
                        "case_id": "review-case",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Run review",
                        "expectation": {
                            "require_success": False,
                            "review": {
                                "expected_outcome": "no_findings",
                                "expect_fix_after_review": False,
                            },
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    suite = load_frozen_suite(path=suite_path)

    assert suite.cases[0].expectation.review is not None
    assert suite.cases[0].expectation.review.expected_outcome == "no_findings"


def test_load_replay_outcomes_accepts_review_payload(tmp_path: Path) -> None:
    replay_path = tmp_path / "review-replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "case-1": {
                    "status": "success",
                    "summary": "review done",
                    "review": {
                        "findings_count": 3,
                        "actionable_findings_count": 2,
                        "false_positive_findings_count": 1,
                        "fix_after_review_attempted": True,
                        "fix_after_review_succeeded": False,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    outcomes = load_replay_outcomes(replay_path)

    assert outcomes["case-1"].review is not None
    assert outcomes["case-1"].review.actionable_findings_count == 2


def test_load_frozen_suite_rejects_non_object_payload(tmp_path: Path) -> None:
    suite_path = tmp_path / "bad-suite.json"
    suite_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="Frozen suite payload validation failed"):
        load_frozen_suite(path=suite_path)


def test_load_frozen_suite_rejects_non_string_required_files_changed(tmp_path: Path) -> None:
    suite_path = tmp_path / "bad-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "bad-suite",
                "cases": [
                    {
                        "case_id": "case-1",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Do one thing",
                        "expectation": {"required_files_changed": ["ok.py", 1]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="required_files_changed"):
        load_frozen_suite(path=suite_path)


def test_load_frozen_suite_rejects_duplicate_case_ids_with_explicit_error(tmp_path: Path) -> None:
    suite_path = tmp_path / "bad-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "bad-suite",
                "cases": [
                    {
                        "case_id": "case-1",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Do one thing",
                        "expectation": {"require_success": True},
                    },
                    {
                        "case_id": "case-1",
                        "repo_fixture": "fixtures/two",
                        "task_text": "Do another thing",
                        "expectation": {"require_success": True},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate case_id found in frozen suite: case-1"):
        load_frozen_suite(path=suite_path)


def test_load_replay_outcomes_rejects_non_object_payload(tmp_path: Path) -> None:
    replay_path = tmp_path / "bad-replay.json"
    replay_path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="Replay payload validation failed"):
        load_replay_outcomes(replay_path)


def test_load_replay_outcomes_rejects_invalid_status(tmp_path: Path) -> None:
    replay_path = tmp_path / "bad-replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "case-1": {
                    "status": "skipped",
                    "summary": "not allowed",
                    "files_changed": [],
                    "tests_passed": True,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="status"):
        load_replay_outcomes(replay_path)


def test_load_frozen_suite_rejects_non_boolean_expectation_flags(tmp_path: Path) -> None:
    suite_path = tmp_path / "bad-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "bad-suite",
                "cases": [
                    {
                        "case_id": "case-1",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Do one thing",
                        "expectation": {"require_success": 1},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="require_success"):
        load_frozen_suite(path=suite_path)


def test_load_replay_outcomes_rejects_non_boolean_tests_passed(tmp_path: Path) -> None:
    replay_path = tmp_path / "bad-replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "case-1": {
                    "status": "success",
                    "summary": "done",
                    "files_changed": [],
                    "tests_passed": 1,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tests_passed"):
        load_replay_outcomes(replay_path)


def test_load_frozen_suite_rejects_unexpected_fields(tmp_path: Path) -> None:
    suite_path = tmp_path / "bad-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "bad-suite",
                "unexpected": "field",
                "cases": [
                    {
                        "case_id": "case-1",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Do one thing",
                        "expectation": {"require_success": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected"):
        load_frozen_suite(path=suite_path)


def test_load_replay_outcomes_rejects_unexpected_fields(tmp_path: Path) -> None:
    replay_path = tmp_path / "bad-replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "case-1": {
                    "status": "success",
                    "summary": "ok",
                    "files_changed": [],
                    "unexpected": "field",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected"):
        load_replay_outcomes(replay_path)


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
    assert comparison.delta_false_positive_rate == pytest.approx(-0.5)


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
        and field_name not in {"delta_passed_cases", "delta_total_score", "delta_reviewed_cases"}
    }
    mapped_comparison_delta_fields = set(_REVIEW_METRIC_TO_COMPARISON_DELTA_FIELD.values())

    assert mapped_metric_fields == review_metric_fields
    assert mapped_comparison_delta_fields == comparison_delta_fields
