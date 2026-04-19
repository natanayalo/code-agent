"""Frozen task-suite loading and replay fixture helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation.harness import FrozenTaskCase, TaskExpectation, WorkerOutcome

_DEFAULT_SUITE_PATH = Path(__file__).with_name("frozen_suite.json")


@dataclass(frozen=True, slots=True)
class FrozenSuite:
    """A loaded and validated frozen benchmark suite."""

    suite_name: str
    cases: tuple[FrozenTaskCase, ...]


def _coerce_string_sequence(raw: Any, *, field_name: str, case_id: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"Case '{case_id}' field '{field_name}' must be a list[str].")
    return tuple(raw)


def load_frozen_suite(path: Path | None = None) -> FrozenSuite:
    """Load the frozen suite definition from JSON and validate structure."""
    suite_path = path or _DEFAULT_SUITE_PATH
    with suite_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError("Frozen suite payload must be a JSON object.")

    suite_name = payload.get("suite_name")
    if not isinstance(suite_name, str) or not suite_name.strip():
        raise ValueError("Frozen suite must define a non-empty suite_name.")

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("Frozen suite must define a list of cases.")

    cases: list[FrozenTaskCase] = []
    seen_case_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("Each frozen suite case must be a JSON object.")

        case_id = raw_case.get("case_id")
        repo_fixture = raw_case.get("repo_fixture")
        task_text = raw_case.get("task_text")
        if (
            not isinstance(case_id, str)
            or not case_id.strip()
            or not isinstance(repo_fixture, str)
            or not repo_fixture.strip()
            or not isinstance(task_text, str)
            or not task_text.strip()
        ):
            raise ValueError(
                "Each case must define non-empty case_id, repo_fixture, and task_text."
            )

        if case_id in seen_case_ids:
            raise ValueError(f"Duplicate case_id found in frozen suite: {case_id}")
        seen_case_ids.add(case_id)

        expectation_payload = raw_case.get("expectation")
        if not isinstance(expectation_payload, dict):
            raise ValueError(f"Case '{case_id}' must define an expectation object.")

        require_success = expectation_payload.get("require_success", True)
        require_tests_passed = expectation_payload.get("require_tests_passed", False)
        if not isinstance(require_success, bool) or not isinstance(require_tests_passed, bool):
            raise ValueError(f"Case '{case_id}' expectation booleans must be true/false values.")

        expectation = TaskExpectation(
            require_success=require_success,
            require_tests_passed=require_tests_passed,
            required_files_changed=_coerce_string_sequence(
                expectation_payload.get("required_files_changed"),
                field_name="required_files_changed",
                case_id=case_id,
            ),
            required_summary_substrings=_coerce_string_sequence(
                expectation_payload.get("required_summary_substrings"),
                field_name="required_summary_substrings",
                case_id=case_id,
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

    return FrozenSuite(suite_name=suite_name, cases=tuple(cases))


def load_replay_outcomes(path: Path) -> dict[str, WorkerOutcome]:
    """Load deterministic replay outcomes for each case id from a JSON file."""
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError("Replay payload must be a JSON object keyed by case id.")

    outcomes: dict[str, WorkerOutcome] = {}
    for case_id, raw_outcome in payload.items():
        if not isinstance(case_id, str) or not isinstance(raw_outcome, dict):
            raise ValueError("Replay payload must map case_id strings to outcome objects.")

        status = raw_outcome.get("status")
        summary = raw_outcome.get("summary")
        files_changed = _coerce_string_sequence(
            raw_outcome.get("files_changed"), field_name="files_changed", case_id=case_id
        )
        tests_passed = raw_outcome.get("tests_passed")
        if status not in {"success", "failure"}:
            raise ValueError(f"Replay outcome for '{case_id}' has invalid status: {status}")
        if not isinstance(summary, str):
            raise ValueError(f"Replay outcome for '{case_id}' must include summary text.")
        if tests_passed is not None and not isinstance(tests_passed, bool):
            raise ValueError(f"Replay outcome for '{case_id}' tests_passed must be bool or null.")

        outcomes[case_id] = WorkerOutcome(
            status=status,
            summary=summary,
            files_changed=files_changed,
            tests_passed=tests_passed,
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
