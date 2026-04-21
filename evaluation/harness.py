"""Core data models and deterministic scoring for frozen-suite evaluations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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

    status: Literal["success", "failure", "error"]
    summary: str
    files_changed: tuple[str, ...] = ()
    tests_passed: bool | None = None


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


async def evaluate_suite(
    *,
    suite_name: str,
    cases: tuple[FrozenTaskCase, ...],
    runner: EvaluationRunner,
    parallel: bool = False,
    max_parallel_cases: int | None = None,
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
