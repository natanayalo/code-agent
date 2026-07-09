"""Unit tests for the memory extraction evaluation harness and CLI runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.pool import StaticPool

from db.base import Base
from evaluation import (
    ExpectedCandidate,
    MemoryExtractionCase,
    MemoryExtractionObservation,
    MemoryExtractionSuite,
    evaluate_memory_extraction,
    load_memory_extraction_suite,
    write_memory_extraction_report,
)
from evaluation.memory_extraction import _match_candidate
from repositories import create_engine_from_url, create_session_factory
from scripts.e2e.run_memory_extraction_eval import main as runner_main


@pytest.fixture
def session_factory():
    """Create an in-memory SQLite session factory for evaluation unit tests."""
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def test_match_candidate() -> None:
    """Verify candidate matching utility checks keys, categories, and values correctly."""
    actual = {
        "memory_key": "conventions",
        "category": "project",
        "value": {"style": "pep8", "indent": 4},
    }

    # Match exact
    assert (
        _match_candidate(actual, ExpectedCandidate(memory_key="conventions", category="project"))
        is True
    )

    # Match with value subset
    assert _match_candidate(actual, ExpectedCandidate(value={"style": "pep8"})) is True

    # Match failure - key mismatch
    assert _match_candidate(actual, ExpectedCandidate(memory_key="pitfalls")) is False

    # Match failure - category mismatch
    assert _match_candidate(actual, ExpectedCandidate(category="personal")) is False

    # Match failure - value mismatch
    assert _match_candidate(actual, ExpectedCandidate(value={"style": "tabs"})) is False


def test_load_memory_extraction_suite(tmp_path: Path) -> None:
    """Verify loading and validation of memory extraction suites."""
    suite_data = {
        "suite_name": "Test Suite",
        "cases": [
            {
                "case_id": "case-1",
                "task_text": "Task 1 text",
                "repo_url": "github.com/foo/bar",
                "observations": [
                    {
                        "source": "worker",
                        "event_type": "worker_completed",
                        "summary": "Completed",
                        "content": "Logs content",
                        "metadata_payload": {"commands_run": []},
                    }
                ],
                "expected_candidates": [
                    {
                        "memory_key": "verification_commands",
                        "category": "project",
                        "value": {"cmd": "pytest"},
                    }
                ],
            }
        ],
    }

    suite_file = tmp_path / "suite.json"
    with suite_file.open("w", encoding="utf-8") as f:
        json.dump(suite_data, f)

    suite = load_memory_extraction_suite(suite_file)
    assert suite.suite_name == "Test Suite"
    assert len(suite.cases) == 1
    assert suite.cases[0].case_id == "case-1"
    assert suite.cases[0].task_text == "Task 1 text"
    assert len(suite.cases[0].observations) == 1
    assert suite.cases[0].observations[0].source == "worker"
    assert len(suite.cases[0].expected_candidates) == 1
    assert suite.cases[0].expected_candidates[0].memory_key == "verification_commands"


def test_load_memory_extraction_suite_validation_failure(tmp_path: Path) -> None:
    """Verify loader raises ValueError for invalid suite structures."""
    invalid_data = {
        "suite_name": "Invalid",
        "cases": [
            {
                "case_id": "",  # Empty id raises error
                "task_text": "Clean",
            }
        ],
    }

    suite_file = tmp_path / "invalid.json"
    with suite_file.open("w", encoding="utf-8") as f:
        json.dump(invalid_data, f)

    with pytest.raises(ValueError, match="Memory extraction suite validation failed"):
        load_memory_extraction_suite(suite_file)


def test_evaluate_memory_extraction(session_factory) -> None:
    """Test evaluation logic runs observation bridging and scores precision/recall correctly."""
    case_1 = MemoryExtractionCase(
        case_id="case-1",
        task_text="Run verification.",
        repo_url="github.com/foo/bar",
        observations=(
            MemoryExtractionObservation(
                source="operator",
                event_type="interaction_resolved",
                summary="Approved rule. Remember to use ruff check.",
                content="Resolving rule approval.",
            ),
        ),
        expected_candidates=(
            ExpectedCandidate(
                memory_key="remembered_instruction",
                category="project",
                value={"instruction": "Remember to use ruff check."},
            ),
        ),
        expected_absent=(),
        expected_admission_decisions=("needs_human_review",),
        expected_proposals=("remembered_instruction",),
    )

    suite = MemoryExtractionSuite(suite_name="Test Suite", cases=(case_1,))
    report = evaluate_memory_extraction(suite, session_factory)

    assert report.suite_name == "Test Suite"
    assert report.total_cases == 1
    assert report.passed_cases == 1
    assert report.failed_cases == 0
    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.quality_metrics["proposal_count"] == 1


def test_write_memory_extraction_report(tmp_path: Path, session_factory) -> None:
    """Verify that reports are written to JSON accurately."""
    case_1 = MemoryExtractionCase(
        case_id="case-1",
        task_text="Run tests.",
        expected_candidates=(),
    )
    suite = MemoryExtractionSuite(suite_name="Test Suite", cases=(case_1,))
    report = evaluate_memory_extraction(suite, session_factory)

    output_path = tmp_path / "report.json"
    write_memory_extraction_report(report, output_path)

    assert output_path.exists()
    with output_path.open("r", encoding="utf-8") as f:
        content = json.load(f)
    assert content["suite_name"] == "Test Suite"
    assert content["passed_cases"] == 1


def test_cli_runner_success(tmp_path: Path) -> None:
    """Verify that the runner script CLI executes successfully and respects thresholds."""
    suite_data = {
        "suite_name": "Test CLI Suite",
        "cases": [
            {
                "case_id": "case-cli",
                "task_text": "Task text",
                "expected_candidates": [],
            }
        ],
    }

    suite_file = tmp_path / "suite.json"
    with suite_file.open("w", encoding="utf-8") as f:
        json.dump(suite_data, f)

    output_file = tmp_path / "report.json"

    test_args = [
        "run_memory_extraction_eval.py",
        "--suite",
        str(suite_file),
        "--output",
        str(output_file),
        "--fail-under-precision",
        "0.9",
        "--fail-under-recall",
        "0.9",
    ]

    with patch.object(sys, "argv", test_args):
        exit_code = runner_main()

    assert exit_code == 0
    assert output_file.exists()


def test_cli_runner_fails_under_threshold(tmp_path: Path) -> None:
    """Verify that the runner exits with code 1 if precision or recall is below threshold."""
    # Seed a case with an expected candidate that will NOT be extracted
    suite_data = {
        "suite_name": "Test CLI Suite",
        "cases": [
            {
                "case_id": "case-cli-fail",
                "task_text": "Task text",
                "expected_candidates": [
                    {
                        "memory_key": "remembered_instruction",
                        "category": "personal",
                        "value": {"instruction": "Remember to run tests."},
                    }
                ],
            }
        ],
    }

    suite_file = tmp_path / "suite.json"
    with suite_file.open("w", encoding="utf-8") as f:
        json.dump(suite_data, f)

    output_file = tmp_path / "report.json"

    # Test fail under recall
    test_args = [
        "run_memory_extraction_eval.py",
        "--suite",
        str(suite_file),
        "--output",
        str(output_file),
        "--fail-under-recall",
        "0.95",
    ]

    with patch.object(sys, "argv", test_args):
        exit_code = runner_main()

    assert exit_code == 1
