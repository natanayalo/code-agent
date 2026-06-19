"""Unit tests for the deterministic frozen evaluation harness (T-106 slice)."""

from __future__ import annotations

import asyncio

import pytest

from evaluation import (
    OrchestratorReplayRunner,
    TaskExpectation,
    WorkerOutcome,
    default_replay_outcomes,
    evaluate_suite,
    load_frozen_suite,
)
from evaluation.harness import (
    FrozenTaskCase,
)


def _make_fake_graph(
    expected_thread_id: str,
    repair_handoff: bool,
    review_outcome: str,
    findings: list[dict[str, object]],
    suppressed: list[dict[str, object]],
    review_summary: str = "reviewed",
) -> object:
    class _FakeGraph:
        async def ainvoke(self, _inputs: object, config: dict[str, object]) -> dict[str, object]:
            assert config["configurable"] == {"thread_id": expected_thread_id}
            return {
                "task": {"task_text": "Do a thing"},
                "dispatch": {"worker_type": "codex"},
                "verification": {"status": "passed", "items": []},
                "repair_handoff_requested": repair_handoff,
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
                    "summary": review_summary,
                    "confidence": 0.8,
                    "outcome": review_outcome,
                    "findings": findings,
                    "suppressed_findings": suppressed,
                },
            }

    return _FakeGraph()


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


def test_orchestrator_runner_auto_approves_non_destructive_unattended_cases() -> None:
    suite = load_frozen_suite()
    case = next(case for case in suite.cases if case.case_id == "frozen-002")
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id=default_replay_outcomes(suite.cases),
        worker_override="codex",
    )

    outcome = asyncio.run(runner.run_case(case))

    assert outcome.status == "success"
    assert "authentication" in outcome.summary
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

    runner._graph = _make_fake_graph(
        expected_thread_id="frozen-eval-reviewed-case",
        repair_handoff=True,
        review_outcome="findings",
        review_summary="one issue surfaced",
        findings=[
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
        suppressed=[
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
    )  # type: ignore[assignment]

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

    runner._graph = _make_fake_graph(
        expected_thread_id="frozen-eval-review-metrics-case",
        repair_handoff=False,
        review_outcome="findings",
        review_summary="reviewed",
        findings=[
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
        suppressed=[
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
    )  # type: ignore[assignment]
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
    assert report.review_metrics.false_discovery_rate == pytest.approx(0.5)
    assert report.review_metrics.false_positive_rate == pytest.approx(0.5)


def test_orchestrator_runner_deduplicates_overlapping_suppressed_findings() -> None:
    case = FrozenTaskCase(
        case_id="overlap-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id={"overlap-case": WorkerOutcome(status="success", summary="ok")},
        worker_override="codex",
    )

    runner._graph = _make_fake_graph(
        expected_thread_id="frozen-eval-overlap-case",
        repair_handoff=False,
        review_outcome="findings",
        review_summary="reviewed",
        findings=[
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
        suppressed=[
            {
                "finding": {
                    "severity": "low",
                    "category": "style",
                    "confidence": 0.6,
                    "file_path": "./src/app.py",
                    "line_start": 12,
                    "line_end": 13,
                    "title": "Missing guard",
                    "why_it_matters": "Can crash on empty input.",
                },
                "reasons": ["style category suppressed by policy (style)"],
            }
        ],
    )  # type: ignore[assignment]
    outcome = asyncio.run(runner.run_case(case))

    assert outcome.review is not None
    assert outcome.review.actionable_findings_count == 0
    assert outcome.review.false_positive_findings_count == 1
    assert outcome.review.findings_count == 1
    assert outcome.review.fix_after_review_attempted is False


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
        worker_override="antigravity",
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
