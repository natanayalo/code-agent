"""Unit tests for the deterministic frozen evaluation harness (T-106 slice)."""

from __future__ import annotations

import json
from pathlib import Path

from evaluation import (
    OrchestratorReplayRunner,
    ReplayRunner,
    TaskExpectation,
    default_replay_outcomes,
    evaluate_suite,
    load_frozen_suite,
    write_report,
)
from evaluation.harness import FrozenTaskCase


def test_frozen_suite_has_minimum_case_count() -> None:
    suite = load_frozen_suite()

    assert suite.suite_name == "frozen-v1"
    assert len(suite.cases) >= 5


def test_evaluate_suite_is_deterministic_for_same_inputs() -> None:
    suite = load_frozen_suite()
    replay_runner = ReplayRunner(default_replay_outcomes(suite.cases))

    report_one = evaluate_suite(
        suite_name=suite.suite_name,
        cases=suite.cases,
        runner=replay_runner,
    )
    report_two = evaluate_suite(
        suite_name=suite.suite_name,
        cases=suite.cases,
        runner=ReplayRunner(default_replay_outcomes(suite.cases)),
    )

    assert report_one.to_dict() == report_two.to_dict()


def test_missing_replay_outcome_is_scored_as_failure() -> None:
    case = FrozenTaskCase(
        case_id="missing-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    report = evaluate_suite(
        suite_name="one-case-suite",
        cases=(case,),
        runner=ReplayRunner(outcomes_by_case_id={}),
    )

    assert report.total_cases == 1
    assert report.failed_cases == 1
    assert report.results[0].outcome.status == "failure"
    assert "missing replay outcome" in report.results[0].outcome.summary.lower()


def test_write_report_persists_structured_json(tmp_path: Path) -> None:
    suite = load_frozen_suite()
    report = evaluate_suite(
        suite_name=suite.suite_name,
        cases=suite.cases,
        runner=ReplayRunner(default_replay_outcomes(suite.cases)),
    )
    output_path = tmp_path / "eval-report.json"

    write_report(report, output_path)

    with output_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    assert payload["suite_name"] == suite.suite_name
    assert payload["total_cases"] == len(suite.cases)
    assert payload["passed_cases"] == len(suite.cases)
    assert payload["results"][0]["case_id"] == suite.cases[0].case_id


def test_orchestrator_runner_executes_case_through_graph_path() -> None:
    suite = load_frozen_suite()
    case = suite.cases[0]
    runner = OrchestratorReplayRunner(
        outcomes_by_case_id=default_replay_outcomes(suite.cases),
        worker_override="codex",
    )

    outcome = runner.run_case(case)

    assert outcome.status == "success"
    assert "zero division" in outcome.summary
    assert set(case.expectation.required_files_changed).issubset(set(outcome.files_changed))
    assert outcome.tests_passed is True


def test_orchestrator_runner_reports_failure_for_missing_case_outcome() -> None:
    case = FrozenTaskCase(
        case_id="missing-case",
        repo_fixture="fixtures/empty",
        task_text="Do a thing",
        expectation=TaskExpectation(require_success=True),
    )
    runner = OrchestratorReplayRunner(outcomes_by_case_id={}, worker_override="codex")

    outcome = runner.run_case(case)

    assert outcome.status == "failure"
    assert "missing replay outcome" in outcome.summary.lower()
