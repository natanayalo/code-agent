"""Unit tests for the deterministic frozen evaluation harness (T-106 slice)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation import (
    load_frozen_suite,
    load_replay_outcomes,
)


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


def test_load_frozen_suite_accepts_review_expectation_payload(tmp_path: Path) -> None:
    suite_path = tmp_path / "review-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "review-suite",
                "cases": [
                    {
                        "case_id": "review-case",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Run review",
                        "expectation": {
                            "require_success": False,
                            "review": {
                                "expected_outcome": "no_findings",
                                "expect_fix_after_review": False,
                            },
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    suite = load_frozen_suite(path=suite_path)

    assert suite.cases[0].expectation.review is not None
    assert suite.cases[0].expectation.review.expected_outcome == "no_findings"


def test_load_replay_outcomes_accepts_review_payload(tmp_path: Path) -> None:
    replay_path = tmp_path / "review-replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "case-1": {
                    "status": "success",
                    "summary": "review done",
                    "review": {
                        "findings_count": 3,
                        "actionable_findings_count": 2,
                        "false_positive_findings_count": 1,
                        "fix_after_review_attempted": True,
                        "fix_after_review_succeeded": False,
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    outcomes = load_replay_outcomes(replay_path)

    assert outcomes["case-1"].review is not None
    assert outcomes["case-1"].review.actionable_findings_count == 2


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


def test_load_frozen_suite_rejects_non_boolean_expectation_flags(tmp_path: Path) -> None:
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
                        "expectation": {"require_success": 1},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="require_success"):
        load_frozen_suite(path=suite_path)


def test_load_replay_outcomes_rejects_non_boolean_tests_passed(tmp_path: Path) -> None:
    replay_path = tmp_path / "bad-replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "case-1": {
                    "status": "success",
                    "summary": "done",
                    "files_changed": [],
                    "tests_passed": 1,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="tests_passed"):
        load_replay_outcomes(replay_path)


def test_load_frozen_suite_rejects_unexpected_fields(tmp_path: Path) -> None:
    suite_path = tmp_path / "bad-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "bad-suite",
                "unexpected": "field",
                "cases": [
                    {
                        "case_id": "case-1",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Do one thing",
                        "expectation": {"require_success": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected"):
        load_frozen_suite(path=suite_path)


def test_load_replay_outcomes_rejects_unexpected_fields(tmp_path: Path) -> None:
    replay_path = tmp_path / "bad-replay.json"
    replay_path.write_text(
        json.dumps(
            {
                "case-1": {
                    "status": "success",
                    "summary": "ok",
                    "files_changed": [],
                    "unexpected": "field",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected"):
        load_replay_outcomes(replay_path)


# ---------------------------------------------------------------------------
# M20.0 task_class loader tests
# ---------------------------------------------------------------------------


def test_load_frozen_suite_accepts_task_class_field(tmp_path: Path) -> None:
    suite_path = tmp_path / "task-class-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "task-class-suite",
                "cases": [
                    {
                        "case_id": "tc-1",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Do something",
                        "task_class": "scout",
                        "expectation": {"require_success": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    suite = load_frozen_suite(path=suite_path)

    assert suite.cases[0].task_class == "scout"


def test_load_frozen_suite_task_class_defaults_to_none(tmp_path: Path) -> None:
    suite_path = tmp_path / "no-task-class-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_name": "no-task-class-suite",
                "cases": [
                    {
                        "case_id": "tc-2",
                        "repo_fixture": "fixtures/one",
                        "task_text": "Do something else",
                        "expectation": {"require_success": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    suite = load_frozen_suite(path=suite_path)

    assert suite.cases[0].task_class is None


def test_load_frozen_suite_task_class_from_production_suite() -> None:
    """At least one production case should have task_class after M20.0 annotation."""
    suite = load_frozen_suite()

    assert any(case.task_class is not None for case in suite.cases)
    # All cases must have task_class as str or None — no crashes.
    for case in suite.cases:
        assert case.task_class is None or isinstance(case.task_class, str)
