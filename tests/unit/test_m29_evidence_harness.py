"""Unit tests for the M29 live provider evidence harness and models."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from evaluation.m29_evidence_models import (
    TOTAL_CASES,
    WAVE3_CASES,
    M29BundleIdentity,
    M29EvidenceBundle,
    M29EvidenceCase,
    M29EvidenceSuite,
)
from orchestrator.nodes.utils import _classify_task_kind
from orchestrator.task_spec import build_task_spec
from scripts.e2e.run_m29_evidence_wave import (
    _get_changed_files_count,
    _resolve_task_failure_kind,
    _submit_case,
    _validate_and_record_outcome,
)

SUITE_PATH = Path("evaluation/m29_live_provider_suite.json")
WAVE3_SUITE_PATH = Path("evaluation/m29_live_provider_suite_wave3.json")
WAVE4_SUITE_PATH = Path("evaluation/m29_live_provider_suite_wave4.json")


def _load_canonical_suite() -> M29EvidenceSuite:
    raw = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    return M29EvidenceSuite.model_validate(raw)


def _load_wave3_suite() -> M29EvidenceSuite:
    """Load the frozen Wave 3 read-only evidence suite."""
    raw = json.loads(WAVE3_SUITE_PATH.read_text(encoding="utf-8"))
    return M29EvidenceSuite.model_validate(raw)


def _load_wave4_suite() -> M29EvidenceSuite:
    """Load the frozen Wave 4 investigation evidence suite."""
    raw = json.loads(WAVE4_SUITE_PATH.read_text(encoding="utf-8"))
    return M29EvidenceSuite.model_validate(raw)


def test_suite_loads_and_has_exact_case_counts() -> None:
    suite = _load_canonical_suite()
    assert len(suite.cases) == TOTAL_CASES

    inv_cases = [c for c in suite.cases if c.task_class == "investigation"]
    feat_cases = [c for c in suite.cases if c.task_class == "feature"]
    docs_cases = [c for c in suite.cases if c.task_class == "docs"]

    assert len(inv_cases) == 2
    assert all(c.worker_profile == "antigravity-native-executor-read-only" for c in inv_cases)

    assert len(feat_cases) == 10
    assert all(c.worker_profile == "antigravity-native-executor-read-only" for c in feat_cases)

    assert len(docs_cases) == 16
    antigravity_docs = [c for c in docs_cases if "antigravity" in c.worker_profile]
    codex_docs = [c for c in docs_cases if "codex" in c.worker_profile]
    assert len(antigravity_docs) == 8
    assert len(codex_docs) == 8


def test_docs_pairs_counterbalance_ordering_bias() -> None:
    suite = _load_canonical_suite()
    docs_cases = [c for c in suite.cases if c.task_class == "docs"]

    groups: dict[str, list[M29EvidenceCase]] = {}
    for c in docs_cases:
        groups.setdefault(c.pair_group or "", []).append(c)

    assert len(groups) == 8
    first_providers = []
    for g_name, cases in groups.items():
        assert len(cases) == 2
        sorted_cases = sorted(cases, key=lambda x: x.pair_order or 0)
        first_providers.append(sorted_cases[0].worker_profile)

    # Assert strict alternation across consecutive groups
    for i in range(len(first_providers) - 1):
        assert (
            first_providers[i] != first_providers[i + 1]
        ), f"Group {i} and {i + 1} did not alternate"


def test_wave3_has_two_balanced_read_only_cells() -> None:
    suite = _load_wave3_suite()
    assert len(suite.cases) == WAVE3_CASES

    for task_class in ("investigation", "feature"):
        cases = [case for case in suite.cases if case.task_class == task_class]
        assert len(cases) == 20
        assert {case.mutation_mode for case in cases} == {"read_only"}
        for profile in (
            "antigravity-native-executor-read-only",
            "codex-native-executor-read-only",
        ):
            assert len([case for case in cases if case.worker_profile == profile]) == 10

        groups: dict[str, list[M29EvidenceCase]] = {}
        for case in cases:
            groups.setdefault(case.pair_group or "", []).append(case)
        assert len(groups) == 10
        first_providers = []
        for pair in groups.values():
            ordered = sorted(pair, key=lambda case: case.pair_order or 0)
            assert ordered[0].topic == ordered[1].topic
            assert ordered[0].prompt == ordered[1].prompt
            first_providers.append(ordered[0].worker_profile)
        assert all(
            first_providers[i] != first_providers[i + 1] for i in range(len(first_providers) - 1)
        )


def test_wave3_prompts_classify_as_safe_declared_task_types() -> None:
    suite = _load_wave3_suite()
    for case in suite.cases:
        kind = _classify_task_kind(case.prompt)
        spec = build_task_spec(
            task_text=case.prompt,
            repo_url=None,
            target_branch=None,
            task_kind=kind,
            constraints={"read_only": True, "delivery_mode": "summary"},
        )
        assert spec.task_type == case.task_class
        assert spec.risk_level == "low"
        assert not spec.requires_clarification
        assert not spec.requires_permission


def test_wave4_has_two_balanced_investigation_cells() -> None:
    suite = _load_wave4_suite()
    assert len(suite.cases) == 20
    assert {case.task_class for case in suite.cases} == {"investigation"}
    assert {case.mutation_mode for case in suite.cases} == {"read_only"}
    for profile in (
        "antigravity-native-executor-read-only",
        "codex-native-executor-read-only",
    ):
        assert len([case for case in suite.cases if case.worker_profile == profile]) == 10

    groups: dict[str, list[M29EvidenceCase]] = {}
    for case in suite.cases:
        groups.setdefault(case.pair_group or "", []).append(case)
    assert len(groups) == 10
    first_providers = []
    for pair in groups.values():
        ordered = sorted(pair, key=lambda case: case.pair_order or 0)
        assert len(pair) == 2
        assert ordered[0].topic == ordered[1].topic
        assert ordered[0].prompt == ordered[1].prompt
        first_providers.append(ordered[0].worker_profile)
    assert all(
        first_providers[i] != first_providers[i + 1] for i in range(len(first_providers) - 1)
    )


def test_wave4_prompts_classify_as_safe_investigations() -> None:
    suite = _load_wave4_suite()
    for case in suite.cases:
        kind = _classify_task_kind(case.prompt)
        spec = build_task_spec(
            task_text=case.prompt,
            repo_url=None,
            target_branch=None,
            task_kind=kind,
            constraints={"read_only": True, "delivery_mode": "summary"},
        )
        assert spec.task_type == "investigation"
        assert spec.risk_level == "low"
        assert not spec.requires_clarification
        assert not spec.requires_permission


def test_all_28_prompts_classify_deterministically_without_gates() -> None:
    suite = _load_canonical_suite()
    for case in suite.cases:
        kind = _classify_task_kind(case.prompt)
        spec = build_task_spec(
            task_text=case.prompt,
            repo_url=None,
            target_branch=None,
            task_kind=kind,
            constraints={"read_only": True, "delivery_mode": "summary"},
        )

        assert (
            spec.task_type == case.task_class
        ), f"Case {case.case_id} classified as '{spec.task_type}', expected '{case.task_class}'"
        assert (
            not spec.requires_clarification
        ), f"Case {case.case_id} unexpectedly requires clarification"
        assert not spec.requires_permission, f"Case {case.case_id} unexpectedly requires permission"
        assert (
            spec.risk_level == "low"
        ), f"Case {case.case_id} risk_level '{spec.risk_level}' != 'low'"
        assert spec.delivery_mode == "summary", f"Case {case.case_id} delivery_mode != 'summary'"


def test_suite_validation_rejects_malformed_distributions() -> None:
    suite = _load_canonical_suite()
    raw = suite.model_dump()

    # Reject duplicate case ID
    raw["cases"][1]["case_id"] = raw["cases"][0]["case_id"]
    with pytest.raises(ValidationError, match="duplicate case IDs"):
        M29EvidenceSuite.model_validate(raw)

    # Reject missing case
    raw_missing = suite.model_dump()
    raw_missing["cases"].pop()
    with pytest.raises(ValidationError, match=f"exactly {TOTAL_CASES} cases"):
        M29EvidenceSuite.model_validate(raw_missing)

    wave3_raw = json.loads(WAVE3_SUITE_PATH.read_text(encoding="utf-8"))
    wave3_raw["cases"][1]["prompt"] = "A different paired prompt without modifying files."
    with pytest.raises(ValidationError, match="must match topic and prompt"):
        M29EvidenceSuite.model_validate(wave3_raw)

    wave4_raw = json.loads(WAVE4_SUITE_PATH.read_text(encoding="utf-8"))
    wave4_raw["cases"][1]["prompt"] = "A different paired prompt without modifying files."
    with pytest.raises(ValidationError, match="must match topic and prompt"):
        M29EvidenceSuite.model_validate(wave4_raw)


def test_bundle_identity_and_outcomes() -> None:
    identity = M29BundleIdentity(
        build_sha="a" * 40,
        target_repository_revision="b" * 40,
        environment="local",
        operator="test-operator",
    )
    bundle = M29EvidenceBundle(
        identity=identity,
        suite_sha256="c" * 64,
        in_flight={},
        cases={},
    )
    assert bundle.identity.build_sha == "a" * 40
    assert bundle.identity.target_repository_revision == "b" * 40
    assert bundle.cases == {}


def test_task_failure_kind_resolution() -> None:
    # 1. From execution plan attempt
    task_attempt = {
        "status": "failed",
        "execution_plan": {"nodes": [{"attempts": [{"failure_kind": "sandbox_timeout"}]}]},
    }
    assert _resolve_task_failure_kind(task_attempt) == "sandbox_timeout"

    # 2. From timeline event
    task_timeline = {
        "status": "failed",
        "timeline": [
            {"event_type": "task_failed", "payload": {"failure_kind": "tool_execution_error"}}
        ],
    }
    assert _resolve_task_failure_kind(task_timeline) == "tool_execution_error"

    # 3. From last error
    task_err = {"status": "failed", "last_error": "Connection refused"}
    assert _resolve_task_failure_kind(task_err) == "task_error"

    # 4. Completed task has None failure_kind
    task_ok = {"status": "completed"}
    assert _resolve_task_failure_kind(task_ok) is None


def test_get_changed_files_count() -> None:
    # Zero files changed
    assert _get_changed_files_count({"latest_run": {"files_changed": []}}) == 0

    # Files changed in latest_run
    assert _get_changed_files_count({"latest_run": {"files_changed": ["README.md"]}}) == 1

    # Files changed in execution plan
    task_plan = {
        "execution_plan": {
            "nodes": [
                {"changed_files": ["foo.py"]},
                {"changed_files": ["bar.py", "baz.py"]},
            ]
        }
    }
    assert _get_changed_files_count(task_plan) == 3


def test_validate_and_record_outcome_success() -> None:
    case = M29EvidenceCase(
        case_id="m29-test-01",
        task_class="investigation",
        mutation_mode="read_only",
        worker_profile="antigravity-native-executor-read-only",
        prompt="Investigate timeline invariants across tasks without modifying files.",
    )
    task_data = {
        "task_id": "task-123",
        "status": "completed",
        "orchestration_runtime": "temporal",
        "runtime_mode": "native_agent",
        "task_spec": {"task_type": "investigation", "delivery_mode": "summary"},
        "chosen_profile": "antigravity-native-executor-read-only",
        "latest_run": {"files_changed": []},
        "created_at": "2026-09-17T20:00:00Z",
        "updated_at": "2026-09-17T20:01:30Z",
    }
    outcome = _validate_and_record_outcome(case, task_data)
    assert outcome.case_id == "m29-test-01"
    assert outcome.task_id == "task-123"
    assert outcome.terminal_status == "completed"
    assert outcome.files_changed_count == 0
    assert outcome.time_to_terminal_seconds == 90.0
    assert outcome.failure_kind is None


def test_validate_and_record_outcome_rejects_violations() -> None:
    case = M29EvidenceCase(
        case_id="m29-test-02",
        task_class="feature",
        mutation_mode="read_only",
        worker_profile="antigravity-native-executor-read-only",
        prompt=(
            "Draft an implementation proposal for provider preflight validation "
            "without modifying files."
        ),
    )

    base = {
        "task_id": "task-456",
        "status": "completed",
        "orchestration_runtime": "temporal",
        "runtime_mode": "native_agent",
        "task_spec": {"task_type": "feature", "delivery_mode": "summary"},
        "chosen_profile": "antigravity-native-executor-read-only",
        "latest_run": {"files_changed": []},
    }

    # Reject non-zero files changed
    with pytest.raises(ValueError, match="modified 1 files"):
        bad_files = dict(base, latest_run={"files_changed": ["modified.py"]})
        _validate_and_record_outcome(case, bad_files)

    # Reject mismatched runtime
    with pytest.raises(ValueError, match="runtime 'docker' != 'temporal'"):
        bad_runtime = dict(base, orchestration_runtime="docker")
        _validate_and_record_outcome(case, bad_runtime)

    # Reject mismatched profile
    with pytest.raises(ValueError, match="profile 'codex-native-executor-read-only'"):
        bad_profile = dict(base, chosen_profile="codex-native-executor-read-only")
        _validate_and_record_outcome(case, bad_profile)

    # Reject mismatched class
    with pytest.raises(ValueError, match="class 'docs' != 'feature'"):
        bad_class = dict(base, task_spec={"task_type": "docs"})
        _validate_and_record_outcome(case, bad_class)


def test_submit_case_constructs_deterministic_payload() -> None:
    client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"task_id": "task-submitted-789"}
    client.post.return_value = mock_resp

    case = M29EvidenceCase(
        case_id="m29-inv-01-extractor-timeline",
        task_class="investigation",
        mutation_mode="read_only",
        worker_profile="antigravity-native-executor-read-only",
        prompt="Investigate extractor deduplication without modifying files.",
    )
    args = MagicMock(repo_key="code-agent", branch="master")

    task_id = _submit_case(client, case, "2026-09-17T20:00:00Z", args)
    assert task_id == "task-submitted-789"

    client.post.assert_called_once()
    _, kwargs = client.post.call_args
    payload = kwargs["json"]

    assert payload["task_text"] == case.prompt
    assert payload["repo_key"] == "code-agent"
    assert payload["branch"] == "master"
    assert payload["worker_override"] == "antigravity"
    assert payload["worker_profile_override"] == "antigravity-native-executor-read-only"
    assert payload["constraints"] == {"read_only": True, "delivery_mode": "summary"}
    assert payload["budget"]["worker_timeout_seconds"] == 600
