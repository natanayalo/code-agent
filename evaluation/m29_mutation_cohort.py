"""Fail-closed provenance checks for the M29 feature/mutation cohort."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

LOGGER = logging.getLogger("m29_mutation_cohort")

M29_MUTATION_PROFILES = ("codex-native-executor", "antigravity-native-executor")
_COHORT_WINDOW = timedelta(days=90)


@dataclass(frozen=True)
class MutationProfileReconciliation:
    """Sanitized counts and hashes for one canonical provider cell."""

    baseline_count: int
    scheduled_suite_count: int
    eligible_suite_count: int
    excluded_suite_count: int
    current_count: int
    baseline_ids_sha256: str
    scheduled_suite_ids_sha256: str
    eligible_suite_ids_sha256: str
    excluded_suite_ids_sha256: str
    current_ids_sha256: str
    reconciliation_status: str = "passed"


@dataclass(frozen=True)
class M29MutationCohortReconciliation:
    """Public-safe result proving an exact, post-cutoff cohort reconciliation."""

    as_of: datetime
    window_start_at: datetime
    window_end_at: datetime
    report_snapshot_verified: bool
    profiles: Mapping[str, MutationProfileReconciliation]
    schema_version: int = 1

    def to_public_dict(self) -> dict[str, object]:
        """Return JSON-compatible reconciliation data without private task IDs."""
        payload = asdict(self)
        for field in ("as_of", "window_start_at", "window_end_at"):
            payload[field] = getattr(self, field).isoformat()
        return payload


def _block(reason: str, profile: str | None = None) -> None:
    LOGGER.warning(
        "M29 mutation cohort reconciliation blocked",
        extra={"reason": reason, "profile": profile},
    )
    raise ValueError(reason)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        _block(f"{field} must be a timezone-aware UTC timestamp")
    return value.astimezone(UTC)


def _ids_by_profile(label: str, source: Mapping[str, Collection[str]]) -> dict[str, set[str]]:
    if set(source) != set(M29_MUTATION_PROFILES):
        _block(f"{label} must contain exactly both canonical provider profiles")
    result: dict[str, set[str]] = {}
    for profile in M29_MUTATION_PROFILES:
        source_values = source[profile]
        if not isinstance(source_values, Collection) or isinstance(source_values, str | bytes):
            _block(f"{label} contains an invalid task ID collection", profile)
        values = list(source_values)
        if any(not isinstance(value, str) or not value for value in values):
            _block(f"{label} contains an invalid task ID", profile)
        if len(values) != len(set(values)):
            _block(f"{label} contains duplicate task IDs", profile)
        result[profile] = set(values)
    return result


def _hash_ids(values: Collection[str]) -> str:
    """Hash sorted IDs as compact UTF-8 JSON with non-ASCII text unescaped."""
    encoded = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _check_disjoint_sets(
    baseline: Mapping[str, set[str]], scheduled: Mapping[str, set[str]]
) -> None:
    baseline_ids = set().union(*baseline.values())
    scheduled_ids = set().union(*scheduled.values())
    if sum(map(len, baseline.values())) != len(baseline_ids):
        _block("baseline task IDs overlap across provider profiles")
    if sum(map(len, scheduled.values())) != len(scheduled_ids):
        _block("scheduled suite task IDs overlap across provider profiles")
    if baseline_ids & scheduled_ids:
        _block("baseline and scheduled suite task IDs must be disjoint")


def _check_terminal_cutoff(
    scheduled: Mapping[str, set[str]],
    terminal_at_by_task_id: Mapping[str, datetime],
    as_of: datetime,
) -> None:
    scheduled_ids = set().union(*scheduled.values())
    if set(terminal_at_by_task_id) != scheduled_ids:
        _block("every scheduled suite task must have one terminal timestamp")
    for terminal_at in terminal_at_by_task_id.values():
        terminal_at = _utc(terminal_at, "suite task terminal timestamp")
        if terminal_at > as_of:
            _block("a scheduled suite task terminalized after the frozen cutoff")


def _check_frozen_baseline_hashes(
    baseline: Mapping[str, set[str]], expected_hashes: Mapping[str, str]
) -> None:
    if set(expected_hashes) != set(M29_MUTATION_PROFILES):
        _block("frozen baseline hashes must contain exactly both provider profiles")
    for profile in M29_MUTATION_PROFILES:
        expected = expected_hashes[profile]
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(char not in "0123456789abcdef" for char in expected)
        ):
            _block("frozen baseline hash must be a lowercase SHA-256 digest", profile)
        if _hash_ids(baseline[profile]) != expected:
            _block("baseline task IDs do not match their frozen SHA-256", profile)


def reconcile_m29_mutation_cohort(
    *,
    as_of: datetime,
    window_start_at: datetime,
    window_end_at: datetime,
    now: datetime,
    baseline_ids_by_profile: Mapping[str, Collection[str]],
    frozen_baseline_ids_sha256_by_profile: Mapping[str, str],
    scheduled_suite_ids_by_profile: Mapping[str, Collection[str]],
    eligible_suite_ids_by_profile: Mapping[str, Collection[str]],
    excluded_suite_ids_by_profile: Mapping[str, Collection[str]],
    terminal_at_by_task_id: Mapping[str, datetime],
    current_ids_by_profile: Mapping[str, Collection[str]],
    report_ids_by_profile: Mapping[str, Collection[str]],
) -> M29MutationCohortReconciliation:
    """Require exact baseline-plus-suite equality before publishing mutation reports."""
    as_of_utc = _utc(as_of, "as_of")
    start_utc = _utc(window_start_at, "window_start_at")
    end_utc = _utc(window_end_at, "window_end_at")
    now_utc = _utc(now, "now")
    if start_utc != as_of_utc - _COHORT_WINDOW or end_utc != as_of_utc:
        _block("mutation cohort must use the frozen 90-day window ending at as_of")
    if now_utc < as_of_utc:
        _block("final cohort reconciliation is not allowed before the frozen cutoff")

    baseline = _ids_by_profile("baseline IDs", baseline_ids_by_profile)
    scheduled = _ids_by_profile("scheduled suite IDs", scheduled_suite_ids_by_profile)
    eligible = _ids_by_profile("eligible suite IDs", eligible_suite_ids_by_profile)
    excluded = _ids_by_profile("excluded suite IDs", excluded_suite_ids_by_profile)
    current = _ids_by_profile("current extractor IDs", current_ids_by_profile)
    report = _ids_by_profile("report extractor IDs", report_ids_by_profile)

    _check_terminal_cutoff(scheduled, terminal_at_by_task_id, as_of_utc)
    _check_disjoint_sets(baseline, scheduled)
    _check_frozen_baseline_hashes(baseline, frozen_baseline_ids_sha256_by_profile)

    profile_results: dict[str, MutationProfileReconciliation] = {}
    for profile in M29_MUTATION_PROFILES:
        if (
            eligible[profile] & excluded[profile]
            or (eligible[profile] | excluded[profile]) != scheduled[profile]
        ):
            _block("every scheduled suite task must be classified exactly once", profile)
        if current[profile] != baseline[profile] | eligible[profile]:
            _block("current extractor IDs do not equal baseline plus eligible suite IDs", profile)
        if report[profile] != current[profile]:
            _block("report snapshot IDs do not match the reconciled extractor IDs", profile)
        profile_results[profile] = MutationProfileReconciliation(
            baseline_count=len(baseline[profile]),
            scheduled_suite_count=len(scheduled[profile]),
            eligible_suite_count=len(eligible[profile]),
            excluded_suite_count=len(excluded[profile]),
            current_count=len(current[profile]),
            baseline_ids_sha256=_hash_ids(baseline[profile]),
            scheduled_suite_ids_sha256=_hash_ids(scheduled[profile]),
            eligible_suite_ids_sha256=_hash_ids(eligible[profile]),
            excluded_suite_ids_sha256=_hash_ids(excluded[profile]),
            current_ids_sha256=_hash_ids(current[profile]),
        )

    return M29MutationCohortReconciliation(
        as_of=as_of_utc,
        window_start_at=start_utc,
        window_end_at=end_utc,
        report_snapshot_verified=True,
        profiles=profile_results,
    )
