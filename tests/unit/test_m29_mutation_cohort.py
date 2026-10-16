"""Coverage for fail-closed M29 mutation cohort reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evaluation.m29_mutation_cohort import (
    M29_MUTATION_PROFILES,
    reconcile_m29_mutation_cohort,
)

AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
PROFILES = M29_MUTATION_PROFILES


def _ids_hash(values: list[str]) -> str:
    encoded = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reconcile(**overrides: Any):
    scheduled = {profile: [f"suite-{profile}", f"excluded-{profile}"] for profile in PROFILES}
    eligible = {profile: [f"suite-{profile}"] for profile in PROFILES}
    excluded = {profile: [f"excluded-{profile}"] for profile in PROFILES}
    baseline = {profile: [f"baseline-{profile}"] for profile in PROFILES}
    current = {profile: [*baseline[profile], *eligible[profile]] for profile in PROFILES}
    terminal = {
        task_id: AS_OF - timedelta(seconds=1)
        for task_ids in scheduled.values()
        for task_id in task_ids
    }
    inputs: dict[str, Any] = {
        "as_of": AS_OF,
        "window_start_at": AS_OF - timedelta(days=90),
        "window_end_at": AS_OF,
        "now": AS_OF + timedelta(seconds=1),
        "baseline_ids_by_profile": baseline,
        "frozen_baseline_ids_sha256_by_profile": {
            profile: _ids_hash(ids) for profile, ids in baseline.items()
        },
        "scheduled_suite_ids_by_profile": scheduled,
        "eligible_suite_ids_by_profile": eligible,
        "excluded_suite_ids_by_profile": excluded,
        "terminal_at_by_task_id": terminal,
        "current_ids_by_profile": current,
        "report_ids_by_profile": {profile: list(ids) for profile, ids in current.items()},
    }
    inputs.update(overrides)
    return reconcile_m29_mutation_cohort(**inputs)


def test_reconcile_returns_only_sanitized_counts_and_hashes() -> None:
    result = _reconcile()

    public = result.to_public_dict()
    assert public["report_snapshot_verified"] is True
    assert set(public["profiles"]) == set(PROFILES)
    for profile, values in public["profiles"].items():
        assert values["baseline_count"] == 1
        assert values["scheduled_suite_count"] == 2
        assert values["eligible_suite_count"] == 1
        assert values["excluded_suite_count"] == 1
        assert values["current_count"] == 2
        assert len(values["current_ids_sha256"]) == 64
        assert "suite-" not in str(values)
        assert profile in PROFILES
    assert "baseline-codex-native-executor" not in str(public)
    assert "excluded-antigravity-native-executor" not in str(public)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("before_cutoff", "before the frozen cutoff"),
        ("wrong_window", "frozen 90-day window"),
        ("after_cutoff_terminal", "after the frozen cutoff"),
        ("missing_terminal", "terminal timestamp"),
        ("unclassified", "classified exactly once"),
        ("baseline_overlap", "must be disjoint"),
        ("extra_current", "baseline plus eligible suite IDs"),
        ("report_snapshot_mismatch", "do not match the reconciled extractor IDs"),
        ("missing_profile", "exactly both canonical provider profiles"),
        ("naive_timestamp", "timezone-aware UTC timestamp"),
        ("invalid_task_id", "invalid task ID"),
        ("invalid_id_collection", "invalid task ID collection"),
        ("duplicate_task_id", "duplicate task IDs"),
        ("baseline_cross_profile_overlap", "overlap across provider profiles"),
        ("suite_cross_profile_overlap", "overlap across provider profiles"),
        ("eligible_excluded_overlap", "classified exactly once"),
        ("non_utc_terminal", "timezone-aware UTC timestamp"),
        ("frozen_baseline_hash_mismatch", "do not match their frozen SHA-256"),
        ("malformed_baseline_hash", "lowercase SHA-256 digest"),
        ("malformed_baseline_hash_type", "lowercase SHA-256 digest"),
        ("missing_baseline_hash", "exactly both provider profiles"),
    ],
)
def test_reconcile_fails_closed_on_incomplete_or_mismatched_evidence(
    change: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _reconcile(**_failure_overrides(change))


def _default_terminal_map(scheduled: dict[str, list[str]]) -> dict[str, datetime]:
    return {
        task_id: AS_OF - timedelta(seconds=1)
        for task_ids in scheduled.values()
        for task_id in task_ids
    }


def _time_failure_overrides(change: str) -> dict[str, Any]:
    scheduled = {profile: [f"suite-{profile}", f"excluded-{profile}"] for profile in PROFILES}
    terminal = _default_terminal_map(scheduled)
    if change == "before_cutoff":
        return {"now": AS_OF - timedelta(seconds=1)}
    if change == "wrong_window":
        return {"window_start_at": AS_OF - timedelta(days=89)}
    if change == "after_cutoff_terminal":
        terminal[f"suite-{PROFILES[0]}"] = AS_OF + timedelta(seconds=1)
    elif change == "missing_terminal":
        terminal.pop(f"suite-{PROFILES[0]}")
    elif change == "non_utc_terminal":
        terminal[f"suite-{PROFILES[0]}"] = AS_OF.replace(tzinfo=None) + timedelta(hours=1)
    elif change == "naive_timestamp":
        return {"now": datetime(2026, 10, 1)}
    else:
        return {}
    return {"terminal_at_by_task_id": terminal}


def _classification_failure_overrides(change: str) -> dict[str, Any]:
    if change == "unclassified":
        return {"excluded_suite_ids_by_profile": {profile: [] for profile in PROFILES}}
    if change == "eligible_excluded_overlap":
        eligible = {profile: [f"suite-{profile}"] for profile in PROFILES}
        eligible[PROFILES[0]].append(f"excluded-{PROFILES[0]}")
        return {"eligible_suite_ids_by_profile": eligible}
    return {}


def _set_overlap_failure_overrides(change: str) -> dict[str, Any]:
    if change == "baseline_overlap":
        baseline = {profile: [f"baseline-{profile}"] for profile in PROFILES}
        baseline[PROFILES[0]] = [f"suite-{PROFILES[0]}"]
        return {"baseline_ids_by_profile": baseline}
    if change == "baseline_cross_profile_overlap":
        return {"baseline_ids_by_profile": {profile: ["shared-baseline"] for profile in PROFILES}}
    if change == "suite_cross_profile_overlap":
        suite = {
            PROFILES[0]: ["suite-one", "shared-suite"],
            PROFILES[1]: ["shared-suite", "excluded-two"],
        }
        return {
            "scheduled_suite_ids_by_profile": suite,
            "terminal_at_by_task_id": _default_terminal_map(suite),
        }
    return {}


def _snapshot_failure_overrides(change: str) -> dict[str, Any]:
    current = {profile: [f"baseline-{profile}", f"suite-{profile}"] for profile in PROFILES}
    if change == "extra_current":
        current[PROFILES[0]].append("unrelated-eligible-task")
        return {"current_ids_by_profile": current, "report_ids_by_profile": current}
    if change == "report_snapshot_mismatch":
        current[PROFILES[0]].append("unrelated-eligible-task")
        return {"report_ids_by_profile": current}
    if change == "missing_profile":
        return {"current_ids_by_profile": {PROFILES[0]: ["baseline", "suite"]}}
    return {}


def _identity_failure_overrides(change: str) -> dict[str, Any]:
    baseline = {profile: [f"baseline-{profile}"] for profile in PROFILES}
    if change == "invalid_task_id":
        baseline[PROFILES[0]] = [""]
    elif change == "invalid_id_collection":
        baseline[PROFILES[0]] = None
    elif change == "duplicate_task_id":
        baseline[PROFILES[0]] = ["duplicate", "duplicate"]
    else:
        return {}
    return {"baseline_ids_by_profile": baseline}


def _baseline_hash_failure_overrides(change: str) -> dict[str, Any]:
    hashes = {profile: _ids_hash([f"baseline-{profile}"]) for profile in PROFILES}
    if change == "frozen_baseline_hash_mismatch":
        hashes[PROFILES[0]] = "0" * 64
    elif change == "malformed_baseline_hash":
        hashes[PROFILES[0]] = "NOT-A-HASH"
    elif change == "malformed_baseline_hash_type":
        hashes[PROFILES[0]] = None
    elif change == "missing_baseline_hash":
        hashes.pop(PROFILES[0])
    else:
        return {}
    return {"frozen_baseline_ids_sha256_by_profile": hashes}


def _failure_overrides(change: str) -> dict[str, Any]:
    for builder in (
        _time_failure_overrides,
        _classification_failure_overrides,
        _set_overlap_failure_overrides,
        _snapshot_failure_overrides,
        _identity_failure_overrides,
        _baseline_hash_failure_overrides,
    ):
        result = builder(change)
        if result:
            return result
    return {}


def test_reconcile_hashes_ids_independently_of_input_order() -> None:
    first = _reconcile()
    second = _reconcile(
        baseline_ids_by_profile={profile: [f"baseline-{profile}"] for profile in PROFILES},
        scheduled_suite_ids_by_profile={
            profile: [f"excluded-{profile}", f"suite-{profile}"] for profile in PROFILES
        },
        eligible_suite_ids_by_profile={profile: [f"suite-{profile}"] for profile in PROFILES},
        excluded_suite_ids_by_profile={profile: [f"excluded-{profile}"] for profile in PROFILES},
        current_ids_by_profile={
            profile: [f"suite-{profile}", f"baseline-{profile}"] for profile in PROFILES
        },
        report_ids_by_profile={
            profile: [f"baseline-{profile}", f"suite-{profile}"] for profile in PROFILES
        },
    )

    assert first.profiles == second.profiles
