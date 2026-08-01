"""Unit coverage for the frozen M25.6 suite contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evaluation.temporal_reliability_capture import load_suite, suite_digest
from evaluation.temporal_reliability_models import ReliabilitySuite, ReliabilitySuiteCase

SUITE_PATH = Path("evaluation/m25_6_reliability_suite.json")


def _suite_payload() -> dict:
    return json.loads(SUITE_PATH.read_text(encoding="utf-8"))


def test_checked_in_suite_has_exact_matrix_and_profiles() -> None:
    suite = load_suite(SUITE_PATH)

    assert len(suite.cases) == 20
    assert sum(case.expected_profile.startswith("codex-") for case in suite.cases) == 10
    assert sum(case.expected_profile.startswith("antigravity-") for case in suite.cases) == 10
    assert not any("openrouter" in case.expected_profile for case in suite.cases)
    assert suite_digest(suite) == suite_digest(load_suite(SUITE_PATH))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload["cases"].pop(), "exactly 20"),
        (
            lambda payload: payload["cases"][1].update({"case_id": payload["cases"][0]["case_id"]}),
            "unique",
        ),
        (
            lambda payload: payload["cases"][0].update(
                {"expected_profile": "openrouter-native-executor-read-only"}
            ),
            "unsupported native profile",
        ),
        (
            lambda payload: payload["cases"][0].update(
                {"expected_profile": "codex-native-executor"}
            ),
            "execution mode disagree",
        ),
        (
            lambda payload: payload["cases"][5].update({"required_proofs": ["validation"]}),
            "missing required proof coverage",
        ),
    ],
)
def test_suite_rejects_matrix_drift(mutation, message: str) -> None:
    payload = _suite_payload()
    mutation(payload)

    with pytest.raises(ValidationError, match=message):
        ReliabilitySuite.model_validate(payload)


def test_suite_rejects_unknown_fields() -> None:
    payload = _suite_payload()
    payload["unexpected"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReliabilitySuite.model_validate(payload)


@pytest.mark.parametrize(
    "case",
    [
        {
            "case_id": "bad-read-only",
            "category": "read_only_monolithic",
            "expected_profile": "codex-native-executor",
            "expected_mode": "mutation",
            "required_proofs": ["validation"],
        },
        {
            "case_id": "bad-fanout",
            "category": "read_only_fanout",
            "expected_profile": "codex-native-executor-read-only",
            "expected_mode": "read_only",
            "required_proofs": [],
        },
        {
            "case_id": "bad-dag",
            "category": "sequential_dag",
            "expected_profile": "codex-native-executor",
            "expected_mode": "mutation",
            "required_proofs": ["validation"],
        },
        {
            "case_id": "bad-draft",
            "category": "draft_pr",
            "expected_profile": "codex-native-executor",
            "expected_mode": "mutation",
            "required_proofs": ["validation"],
        },
        {
            "case_id": "bad-mutation",
            "category": "mutation",
            "expected_profile": "codex-native-executor",
            "expected_mode": "mutation",
            "required_proofs": [],
        },
    ],
)
def test_case_contract_rejects_missing_scenario_proof(case: dict) -> None:
    with pytest.raises(ValidationError):
        ReliabilitySuiteCase.model_validate(case)


def test_suite_rejects_category_and_profile_allocation_drift() -> None:
    category_payload = _suite_payload()
    category_payload["cases"][0]["category"] = "mutation"
    category_payload["cases"][0]["expected_profile"] = "codex-native-executor"
    category_payload["cases"][0]["expected_mode"] = "mutation"
    category_payload["cases"][0]["required_proofs"] = ["validation"]
    with pytest.raises(ValidationError, match="category counts"):
        ReliabilitySuite.model_validate(category_payload)

    profile_payload = _suite_payload()
    profile_payload["cases"][4]["expected_profile"] = "antigravity-native-executor"
    with pytest.raises(ValidationError, match="profile allocation"):
        ReliabilitySuite.model_validate(profile_payload)
