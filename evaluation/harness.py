"""Core data models and deterministic scoring for frozen-suite evaluations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class TaskExpectation:
    """Expected output constraints for one frozen evaluation case."""

    require_success: bool = True
    require_tests_passed: bool = False
    required_files_changed: tuple[str, ...] = ()
    required_summary_substrings: tuple[str, ...] = ()


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

    status: Literal["success", "failure"]
    summary: str
    files_changed: tuple[str, ...] = ()
    tests_passed: bool | None = None


class EvaluationRunner(Protocol):
    """Runner boundary for orchestrator/replay adapters used by the harness."""

    def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
        """Execute one case and return the normalized outcome."""


class ReplayRunner:
    """Deterministic adapter that replays pre-baked outcomes by case id."""

    def __init__(self, outcomes_by_case_id: dict[str, WorkerOutcome]) -> None:
        self._outcomes_by_case_id = dict(outcomes_by_case_id)

    def run_case(self, case: FrozenTaskCase) -> WorkerOutcome:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_name": self.suite_name,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "total_score": self.total_score,
            "max_score": self.max_score,
            "results": [result.to_dict() for result in self.results],
        }


def _score_case(case: FrozenTaskCase, outcome: WorkerOutcome) -> CaseRunResult:
    points = 0
    max_points = (
        1
        + int(case.expectation.require_tests_passed)
        + len(case.expectation.required_files_changed)
        + len(case.expectation.required_summary_substrings)
    )
    failures: list[str] = []

    if not case.expectation.require_success or outcome.status == "success":
        points += 1
    else:
        failures.append("status was not success")

    if case.expectation.require_tests_passed:
        if outcome.tests_passed is True:
            points += 1
        else:
            failures.append("tests were expected to pass")

    changed_files = set(outcome.files_changed)
    for required_file in case.expectation.required_files_changed:
        if required_file in changed_files:
            points += 1
        else:
            failures.append(f"required file was not changed: {required_file}")

    summary_text = outcome.summary.lower()
    for required_substring in case.expectation.required_summary_substrings:
        if required_substring.lower() in summary_text:
            points += 1
        else:
            failures.append("required summary substring missing: " f"{required_substring}")

    return CaseRunResult(
        case_id=case.case_id,
        passed=points == max_points,
        score=points,
        max_score=max_points,
        failures=tuple(failures),
        outcome=outcome,
    )


def evaluate_suite(
    *,
    suite_name: str,
    cases: tuple[FrozenTaskCase, ...],
    runner: EvaluationRunner,
) -> EvaluationReport:
    """Execute and score all frozen cases through the supplied runner."""
    results = tuple(_score_case(case, runner.run_case(case)) for case in cases)
    total_cases = len(results)
    passed_cases = sum(1 for result in results if result.passed)
    failed_cases = total_cases - passed_cases
    total_score = sum(result.score for result in results)
    max_score = sum(result.max_score for result in results)

    return EvaluationReport(
        suite_name=suite_name,
        total_cases=total_cases,
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        total_score=total_score,
        max_score=max_score,
        results=results,
    )


def write_report(report: EvaluationReport, output_path: Path) -> None:
    """Persist a deterministic JSON report for local and CI diffing."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(report.to_dict(), file, indent=2, sort_keys=True)
        file.write("\n")
