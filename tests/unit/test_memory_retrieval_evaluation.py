"""Unit tests for deterministic memory retrieval evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from evaluation import (
    evaluate_memory_retrieval,
    load_memory_retrieval_suite,
    write_memory_retrieval_report,
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


def _write_suite(path: Path, *, cases: list[dict]) -> None:
    path.write_text(
        json.dumps(
            {
                "suite_name": "memory-eval-test",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "personal_memory": [
                    {
                        "memory_key": "communication_style",
                        "value": {"style": "concise"},
                        "source": "operator",
                        "confidence": 0.9,
                        "scope": "global",
                        "requires_verification": False,
                    }
                ],
                "project_memory": [
                    {
                        "memory_key": "pytest_matrix",
                        "value": {"cmd": ".venv/bin/pytest", "purpose": "pytest"},
                    },
                    {
                        "memory_key": "coverage_gate",
                        "value": {"purpose": "coverage threshold"},
                    },
                ],
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )


def test_load_memory_retrieval_suite_rejects_unexpected_fields(tmp_path: Path) -> None:
    suite_path = tmp_path / "bad-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "bad",
                "repo_url": "https://github.com/natanayalo/code-agent",
                "cases": [],
                "surprise": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Memory retrieval suite validation failed"):
        load_memory_retrieval_suite(suite_path)


def test_load_memory_retrieval_suite_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    suite_path = tmp_path / "duplicate-suite.json"
    _write_suite(
        suite_path,
        cases=[
            {"case_id": "same", "task_text": "pytest"},
            {"case_id": "same", "task_text": "concise"},
        ],
    )

    with pytest.raises(
        ValueError,
        match="Duplicate case_id found in memory retrieval suite: same",
    ):
        load_memory_retrieval_suite(suite_path)


def test_evaluate_memory_retrieval_splits_known_semantic_gap_misses(
    tmp_path: Path,
) -> None:
    suite_path = tmp_path / "suite.json"
    _write_suite(
        suite_path,
        cases=[
            {
                "case_id": "b-known-gap",
                "task_text": "pytest",
                "expected_project_keys": ["coverage_gate", "pytest_matrix"],
                "known_semantic_gap_project_keys": ["coverage_gate"],
            },
            {
                "case_id": "a-direct-personal",
                "task_text": "concise",
                "expected_personal_keys": ["communication_style"],
            },
        ],
    )
    suite = load_memory_retrieval_suite(suite_path)

    report = evaluate_memory_retrieval(
        suite=suite,
        session_factory=_sqlite_session_factory(),
        search_limit=7,
    )

    assert report.recall == 1.0
    assert report.regression_misses == ()
    assert report.known_semantic_gap_misses == ("b-known-gap:project:coverage_gate",)
    assert [result.case_id for result in report.results] == [
        "a-direct-personal",
        "b-known-gap",
    ]
    direct_result = report.results[0]
    assert direct_result.memory_loaded_payload.retrieval_mode == "full_text"
    assert direct_result.memory_loaded_payload.search_query == "concise"
    assert direct_result.memory_loaded_payload.search_limit == 7
    assert direct_result.memory_loaded_payload.personal_keys == ("communication_style",)


def test_evaluate_memory_retrieval_reports_regression_misses(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    _write_suite(
        suite_path,
        cases=[
            {
                "case_id": "missing",
                "task_text": "no-match",
                "expected_personal_keys": ["communication_style"],
            }
        ],
    )
    suite = load_memory_retrieval_suite(suite_path)

    report = evaluate_memory_retrieval(
        suite=suite,
        session_factory=_sqlite_session_factory(),
    )

    assert report.recall == 0.0
    assert report.cases_with_regression_misses == 1
    assert report.regression_misses == ("missing:personal:communication_style",)
    assert report.results[0].passed is False


def test_write_memory_retrieval_report_is_sorted_and_newline_terminated(
    tmp_path: Path,
) -> None:
    suite = load_memory_retrieval_suite()
    report = evaluate_memory_retrieval(
        suite=suite,
        session_factory=_sqlite_session_factory(),
    )
    output_path = tmp_path / "report.json"

    write_memory_retrieval_report(report, output_path)

    text = output_path.read_text(encoding="utf-8")
    payload = json.loads(text)

    assert text.endswith("\n")
    assert text.splitlines()[1] == '  "cases_with_full_recall": 2,'
    assert [result["case_id"] for result in payload["results"]] == sorted(
        result["case_id"] for result in payload["results"]
    )
    assert payload["known_semantic_gap_misses"] == sorted(payload["known_semantic_gap_misses"])


def test_realistic_memory_retrieval_suite_has_deterministic_result() -> None:
    suite = load_memory_retrieval_suite(Path("evaluation/memory_retrieval_realistic_suite.json"))

    report = evaluate_memory_retrieval(
        suite=suite,
        session_factory=_sqlite_session_factory(),
    )

    assert suite.suite_name == "memory-retrieval-m23-slice-4-realistic"
    assert len(suite.personal_memory) + len(suite.project_memory) == 12
    assert report.recall == 1.0
    assert report.regression_misses == ()
    assert report.known_semantic_gap_misses == (
        "known-gap-definition-of-done:project:definition_of_done",
        "known-gap-migration-validation-synonym:project:db_migration_policy",
        "known-gap-worker-boundary-synonym:project:worker_boundaries",
    )
