"""Frozen task-suite loading and replay fixture helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError, field_validator

from evaluation.harness import FrozenTaskCase, TaskExpectation, WorkerOutcome

_DEFAULT_SUITE_PATH = Path(__file__).with_name("frozen_suite.json")


@dataclass(frozen=True, slots=True)
class FrozenSuite:
    """A loaded and validated frozen benchmark suite."""

    suite_name: str
    cases: tuple[FrozenTaskCase, ...]


class _ExpectationPayload(BaseModel):
    require_success: bool = True
    require_tests_passed: bool = False
    required_files_changed: list[str] | None = None
    required_summary_substrings: list[str] | None = None


class _CasePayload(BaseModel):
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
    suite_name: str
    cases: list[_CasePayload]

    @field_validator("suite_name")
    @classmethod
    def _require_non_empty_suite_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class _ReplayOutcomePayload(BaseModel):
    status: Literal["success", "failure", "error"]
    summary: str
    files_changed: list[str] | None = None
    tests_passed: bool | None = None


class _ReplayPayload(BaseModel):
    outcomes: dict[str, _ReplayOutcomePayload]


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
            "Frozen suite payload validation failed: " f"{_summarize_validation_error(exc)}"
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
        parsed = _ReplayPayload.model_validate({"outcomes": payload})
    except ValidationError as exc:
        raise ValueError(
            "Replay payload validation failed: " f"{_summarize_validation_error(exc)}"
        ) from exc

    outcomes: dict[str, WorkerOutcome] = {}
    for case_id, raw_outcome in parsed.outcomes.items():
        outcomes[case_id] = WorkerOutcome(
            status=raw_outcome.status,
            summary=raw_outcome.summary,
            files_changed=tuple(raw_outcome.files_changed or ()),
            tests_passed=raw_outcome.tests_passed,
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
        )
    return outcomes
