"""Unit coverage for the M28 paired memory-effectiveness evaluator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool

import orchestrator.graph as graph_module
from db.base import Base
from evaluation.memory_effectiveness import (
    evaluate_memory_effectiveness,
    load_memory_effectiveness_suite,
    write_memory_effectiveness_report,
)
from repositories import create_engine_from_url, create_session_factory


def _sqlite_session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_load_memory_effectiveness_suite_rejects_incomplete_scenario_matrix(tmp_path: Path) -> None:
    payload = json.loads(Path("evaluation/m28_memory_effectiveness_suite.json").read_text())
    payload["cases"] = payload["cases"][:-1]
    suite_path = tmp_path / "incomplete.json"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="include each M28 scenario exactly once"):
        load_memory_effectiveness_suite(suite_path)


def test_load_memory_effectiveness_suite_rejects_unexpected_fields(tmp_path: Path) -> None:
    payload = json.loads(Path("evaluation/m28_memory_effectiveness_suite.json").read_text())
    payload["unexpected"] = True
    suite_path = tmp_path / "unexpected.json"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        load_memory_effectiveness_suite(suite_path)


def test_load_memory_effectiveness_suite_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    payload = json.loads(Path("evaluation/m28_memory_effectiveness_suite.json").read_text())
    payload["cases"][1]["case_id"] = payload["cases"][0]["case_id"]
    suite_path = tmp_path / "duplicates.json"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="case IDs must be unique"):
        load_memory_effectiveness_suite(suite_path)


def test_evaluate_memory_effectiveness_captures_all_lifecycle_outcomes() -> None:
    report = evaluate_memory_effectiveness(
        suite=load_memory_effectiveness_suite(),
        session_factory=_sqlite_session_factory(),
    )

    assert report.status == "passed"
    assert report.passed_cases == 4
    assert report.failed_case_ids == []
    outcomes = {result.case_id: result for result in report.results}
    assert (
        outcomes["useful-hit"].assisted.candidates[0].context_disposition == "available_to_worker"
    )
    assert outcomes["irrelevant-rejection"].assisted.candidates[0].reason_codes == [
        "not_retrieved_for_query"
    ]
    assert (
        outcomes["stale-reverification"].assisted.candidates[0].context_disposition == "suppressed"
    )
    conflict_candidates = outcomes["conflict-handling"].assisted.candidates
    assert [candidate.context_disposition for candidate in conflict_candidates] == [
        "suppressed",
        "available_to_worker",
    ]
    assert all(
        not result.cold.personal_keys and not result.cold.project_keys and not result.cold.session
        for result in report.results
    )
    assert all(result.session_continuity.passed for result in report.results)


def test_evaluate_memory_effectiveness_reports_failed_expectation() -> None:
    suite = load_memory_effectiveness_suite()
    useful_case = next(case for case in suite.cases if case.case_id == "useful-hit")
    useful_expected = useful_case.expected_candidates[0].model_copy(
        update={"context_disposition": "suppressed"}
    )
    updated_case = useful_case.model_copy(update={"expected_candidates": [useful_expected]})
    updated_suite = suite.model_copy(
        update={
            "cases": [
                updated_case if case.case_id == useful_case.case_id else case
                for case in suite.cases
            ]
        }
    )

    report = evaluate_memory_effectiveness(
        suite=updated_suite,
        session_factory=_sqlite_session_factory(),
    )

    assert report.status == "failed"
    assert report.failed_case_ids == ["useful-hit"]
    assert (
        "candidate:project:m28_verified_test_matrix:context_disposition"
        in next(result for result in report.results if result.case_id == "useful-hit").failures
    )


def test_evaluate_memory_effectiveness_detects_load_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_mapper = graph_module._memory_entry_from_row

    def mapper_with_drift(row):
        entry = original_mapper(row)
        if entry.memory_key == "m28_verified_test_matrix":
            return entry.model_copy(
                update={
                    "source": "corrupted_in_load",
                    "scope": "branch",
                    "last_verified_at": None,
                }
            )
        if entry.memory_key == "m28_deployment_policy":
            return entry.model_copy(
                update={
                    "source": "corrupted_in_load",
                    "scope": "branch",
                    "last_verified_at": None,
                }
            )
        return entry

    monkeypatch.setattr(graph_module, "_memory_entry_from_row", mapper_with_drift)

    report = evaluate_memory_effectiveness(
        suite=load_memory_effectiveness_suite(),
        session_factory=_sqlite_session_factory(),
    )

    useful = next(result for result in report.results if result.case_id == "useful-hit")
    candidate = useful.assisted.candidates[0]
    assert candidate.source == "corrupted_in_load"
    assert candidate.scope == "branch"
    assert candidate.verification_state == "unverified"
    assert useful.passed is False
    assert "candidate:project:m28_verified_test_matrix:source" in useful.failures
    assert "candidate:project:m28_verified_test_matrix:scope" in useful.failures
    assert "candidate:project:m28_verified_test_matrix:verification_state" in useful.failures
    stale = next(result for result in report.results if result.case_id == "stale-reverification")
    suppressed_candidate = stale.assisted.candidates[0]
    assert suppressed_candidate.source == "corrupted_in_load"
    assert suppressed_candidate.scope == "branch"
    assert suppressed_candidate.verification_state == "unverified"
    assert stale.passed is False
    assert "candidate:project:m28_deployment_policy:source" in stale.failures
    assert "candidate:project:m28_deployment_policy:scope" in stale.failures
    assert "candidate:project:m28_deployment_policy:verification_state" in stale.failures


def test_write_memory_effectiveness_report_is_sorted_and_newline_terminated(tmp_path: Path) -> None:
    report = evaluate_memory_effectiveness(
        suite=load_memory_effectiveness_suite(),
        session_factory=_sqlite_session_factory(),
    )
    output_path = tmp_path / "report.json"

    write_memory_effectiveness_report(report, output_path)

    text = output_path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert text.endswith("\n")
    assert text.splitlines()[1] == '  "failed_case_ids": [],'
    assert [result["case_id"] for result in payload["results"]] == sorted(
        result["case_id"] for result in payload["results"]
    )
