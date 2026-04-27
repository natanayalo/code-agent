"""Frozen task-suite loading and replay fixture helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator

from evaluation.harness import (
    FrozenTaskCase,
    ReviewExpectation,
    ReviewOutcome,
    TaskExpectation,
    WorkerOutcome,
)

_DEFAULT_SUITE_PATH = Path(__file__).with_name("frozen_suite.json")


@dataclass(frozen=True, slots=True)
class FrozenSuite:
    """A loaded and validated frozen benchmark suite."""

    suite_name: str
    cases: tuple[FrozenTaskCase, ...]


class _ExpectationPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    require_success: bool = True
    require_tests_passed: bool = False
    required_files_changed: list[str] | None = None
    required_summary_substrings: list[str] | None = None
    review: _ReviewExpectationPayload | None = None


class _ReviewExpectationPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    expected_outcome: Literal["no_findings", "findings"] | None = None
    expect_fix_after_review: bool | None = None


class _CasePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    case_id: str
    repo_fixture: str
    task_text: str
    expectation: _ExpectationPayload

    @field_validator("case_id", "repo_fixture", "task_text")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class _FrozenSuitePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    suite_name: str
    cases: list[_CasePayload]

    @field_validator("suite_name")
    @classmethod
    def _require_non_empty_suite_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class _ReplayOutcomePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["success", "failure", "error"]
    summary: str
    files_changed: list[str] | None = None
    tests_passed: bool | None = None
    review: _ReviewOutcomePayload | None = None


class _ReviewOutcomePayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    findings_count: int = 0
    actionable_findings_count: int = 0
    false_positive_findings_count: int = 0
    fix_after_review_attempted: bool | None = None
    fix_after_review_succeeded: bool | None = None


_REPLAY_OUTCOMES_ADAPTER = TypeAdapter(dict[str, _ReplayOutcomePayload])


def _summarize_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_input=False)
    details: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid value"))
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        details.append(f"{location}: {message}" if location else message)
    return "; ".join(details) if details else "invalid payload"


def load_frozen_suite(path: Path | None = None) -> FrozenSuite:
    """Load the frozen suite definition from JSON and validate structure."""
    suite_path = path or _DEFAULT_SUITE_PATH
    with suite_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    try:
        parsed = _FrozenSuitePayload.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            f"Frozen suite payload validation failed: {_summarize_validation_error(exc)}"
        ) from exc

    cases: list[FrozenTaskCase] = []
    seen_case_ids: set[str] = set()
    for raw_case in parsed.cases:
        case_id = raw_case.case_id
        repo_fixture = raw_case.repo_fixture
        task_text = raw_case.task_text

        if case_id in seen_case_ids:
            raise ValueError(f"Duplicate case_id found in frozen suite: {case_id}")
        seen_case_ids.add(case_id)

        expectation_payload = raw_case.expectation

        expectation = TaskExpectation(
            require_success=expectation_payload.require_success,
            require_tests_passed=expectation_payload.require_tests_passed,
            required_files_changed=tuple(expectation_payload.required_files_changed or ()),
            required_summary_substrings=tuple(
                expectation_payload.required_summary_substrings or ()
            ),
            review=(
                None
                if expectation_payload.review is None
                else ReviewExpectation(
                    expected_outcome=expectation_payload.review.expected_outcome,
                    expect_fix_after_review=expectation_payload.review.expect_fix_after_review,
                )
            ),
        )

        cases.append(
            FrozenTaskCase(
                case_id=case_id,
                repo_fixture=repo_fixture,
                task_text=task_text,
                expectation=expectation,
            )
        )

    return FrozenSuite(suite_name=parsed.suite_name, cases=tuple(cases))


def load_replay_outcomes(path: Path) -> dict[str, WorkerOutcome]:
    """Load deterministic replay outcomes for each case id from a JSON file."""
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    try:
        parsed = _REPLAY_OUTCOMES_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise ValueError(
            f"Replay payload validation failed: {_summarize_validation_error(exc)}"
        ) from exc

    outcomes: dict[str, WorkerOutcome] = {}
    for case_id, raw_outcome in parsed.items():
        outcomes[case_id] = WorkerOutcome(
            status=raw_outcome.status,
            summary=raw_outcome.summary,
            files_changed=tuple(raw_outcome.files_changed or ()),
            tests_passed=raw_outcome.tests_passed,
            review=(
                None
                if raw_outcome.review is None
                else ReviewOutcome(
                    findings_count=raw_outcome.review.findings_count,
                    actionable_findings_count=raw_outcome.review.actionable_findings_count,
                    false_positive_findings_count=raw_outcome.review.false_positive_findings_count,
                    fix_after_review_attempted=raw_outcome.review.fix_after_review_attempted,
                    fix_after_review_succeeded=raw_outcome.review.fix_after_review_succeeded,
                )
            ),
        )

    return outcomes


def default_replay_outcomes(cases: tuple[FrozenTaskCase, ...]) -> dict[str, WorkerOutcome]:
    """Generate deterministic pass-path replay outcomes from the frozen case expectations."""
    outcomes: dict[str, WorkerOutcome] = {}
    for case in cases:
        suffix_parts = list(case.expectation.required_summary_substrings)
        if not suffix_parts:
            suffix_parts.append("all acceptance checks passed")
        outcomes[case.case_id] = WorkerOutcome(
            status="success",
            summary="; ".join(suffix_parts),
            files_changed=case.expectation.required_files_changed,
            tests_passed=True if case.expectation.require_tests_passed else None,
            review=(
                None
                if case.expectation.review is None
                else ReviewOutcome(
                    findings_count=(
                        0 if case.expectation.review.expected_outcome == "no_findings" else 1
                    ),
                    actionable_findings_count=(
                        0 if case.expectation.review.expected_outcome == "no_findings" else 1
                    ),
                    false_positive_findings_count=0,
                    fix_after_review_attempted=(
                        True if case.expectation.review.expect_fix_after_review else None
                    ),
                    fix_after_review_succeeded=(
                        True if case.expectation.review.expect_fix_after_review else None
                    ),
                )
            ),
        )
    return outcomes
