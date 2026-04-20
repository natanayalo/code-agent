"""Unit tests for the deterministic frozen evaluation harness (T-106 slice)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evaluation import (
    OrchestratorReplayRunner,
    ReplayRunner,
    TaskExpectation,
    WorkerOutcome,
    default_replay_outcomes,
    evaluate_suite,
    load_frozen_suite,
    load_replay_outcomes,
    write_report,
)
from evaluation.harness import FrozenTaskCase


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
