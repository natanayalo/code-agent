"""Sanitized M29 feature/mutation manifest contracts and paired analysis."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from evaluation.m29_mutation_cohort import M29_MUTATION_PROFILES, M29MutationCohortReconciliation
from evaluation.m29_mutation_suite import M29_MUTATION_SUITE_NAME, M29MutationProfile
from evaluation.provider_reliability_models import StrictModel
from evaluation.provider_reliability_report import UNSAFE_VALUE_SUBSTRINGS


class M29MutationManifestCase(StrictModel):
    """Public, task-id-free outcome for one frozen M29 feature/mutation case."""

    case_id: str = Field(pattern=r"^[a-z0-9-]+$")
    pair_group: str = Field(pattern=r"^[a-z0-9-]+$")
    pair_order: int = Field(ge=1, le=2)
    topic: str = Field(pattern=r"^[a-z0-9_]+$")
    task_class: Literal["feature"] = "feature"
    mutation_mode: Literal["mutation"] = "mutation"
    worker_profile: M29MutationProfile
    provider: Literal["codex", "antigravity"]

    # Pre-evaluation task completion (canonical extractor outcome)
    terminal_status: Literal["completed", "failed"]
    accepted: bool
    failure_kind: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    report_failure_kind: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    time_to_terminal_seconds: float | None = Field(default=None, ge=0.0)
    execution_identity_status: Literal[
        "verified", "unknown_execution_identity", "mixed_execution_identity"
    ]
    identity_matches: bool
    exclusion_reason: str | None = None

    # Post-terminal independent feature quality (external evaluation)
    quality_status: Literal["pass", "fail", "not_assessable"]
    acceptance_oracle_passed: bool
    regression_suite_passed: bool
    neutral_review_passed: bool
    evaluator_identity_status: Literal["verified", "mismatched", "unavailable"]
    evaluator_identity_matches: bool
    quality_reason: str | None = None

    @model_validator(mode="after")
    def validate_case_invariants(self) -> M29MutationManifestCase:
        _validate_provider_profile(self)
        _validate_pre_evaluation_completion(self)
        _validate_independent_quality(self)
        return self


def _validate_provider_profile(case: M29MutationManifestCase) -> None:
    expected_provider = "codex" if case.worker_profile == "codex-native-executor" else "antigravity"
    if case.provider != expected_provider:
        raise ValueError(
            f"case '{case.case_id}' provider '{case.provider}' does not match "
            f"worker profile '{case.worker_profile}' (expected '{expected_provider}')"
        )


def _validate_pre_evaluation_completion(case: M29MutationManifestCase) -> None:
    if case.accepted:
        if case.terminal_status != "completed":
            raise ValueError(
                f"case '{case.case_id}' accepted cannot be True when terminal_status is "
                f"'{case.terminal_status}'"
            )
        if case.report_failure_kind is not None:
            raise ValueError(
                f"accepted case '{case.case_id}' cannot have report_failure_kind "
                f"'{case.report_failure_kind}'"
            )
        if case.exclusion_reason is not None:
            raise ValueError(
                f"accepted case '{case.case_id}' cannot have exclusion_reason "
                f"'{case.exclusion_reason}'"
            )
        if not case.identity_matches or case.execution_identity_status != "verified":
            raise ValueError(f"accepted case '{case.case_id}' requires verified matching identity")
    else:
        if case.exclusion_reason is None and case.terminal_status == "completed":
            raise ValueError(f"non-excluded completed case '{case.case_id}' must be accepted")
        if case.exclusion_reason is None and case.report_failure_kind is None:
            raise ValueError(f"failed eligible case '{case.case_id}' requires report_failure_kind")


def _validate_independent_quality(case: M29MutationManifestCase) -> None:
    if not case.evaluator_identity_matches or case.evaluator_identity_status != "verified":
        if case.quality_status != "not_assessable":
            raise ValueError(
                f"case '{case.case_id}' quality_status must be 'not_assessable' when "
                "evaluator identity is not verified or does not match"
            )
        if case.quality_reason is None:
            raise ValueError(
                f"case '{case.case_id}' requires quality_reason when quality is not assessable"
            )
    elif case.quality_status == "pass":
        if not (
            case.acceptance_oracle_passed
            and case.regression_suite_passed
            and case.neutral_review_passed
        ):
            raise ValueError(
                f"case '{case.case_id}' quality_status cannot be 'pass' when acceptance, "
                "regression, or neutral review checks fail"
            )
        if case.quality_reason is not None:
            raise ValueError(f"case '{case.case_id}' passing quality cannot have quality_reason")
    elif case.quality_status == "fail":
        if (
            case.acceptance_oracle_passed
            and case.regression_suite_passed
            and case.neutral_review_passed
        ):
            raise ValueError(
                f"case '{case.case_id}' quality_status cannot be 'fail' when all checks pass"
            )
        if case.quality_reason is None:
            raise ValueError(f"failing quality case '{case.case_id}' requires quality_reason")
    elif case.quality_status == "not_assessable" and case.quality_reason is None:
        raise ValueError(
            f"case '{case.case_id}' requires quality_reason when quality is not assessable"
        )


class M29MutationPairedTaskSummary(StrictModel):
    """Paired descriptive outcomes for the 10 counterbalanced feature/mutation pairs."""

    pair_count: int = Field(ge=0)
    both_completed_count: int = Field(ge=0)
    codex_only_completed_count: int = Field(ge=0)
    antigravity_only_completed_count: int = Field(ge=0)
    neither_completed_count: int = Field(ge=0)
    both_quality_pass_count: int = Field(ge=0)
    codex_only_quality_pass_count: int = Field(ge=0)
    antigravity_only_quality_pass_count: int = Field(ge=0)
    neither_quality_pass_count: int = Field(ge=0)
    identity_complete_pair_count: int = Field(ge=0)


def analyze_mutation_paired_manifest(
    manifest: M29MutationManifest,
) -> M29MutationPairedTaskSummary:
    """Compute paired completion and independent quality counts across the 10 topic pairs."""
    pairs: dict[str, dict[str, M29MutationManifestCase]] = {}
    for case in manifest.cases:
        pairs.setdefault(case.pair_group, {})[case.provider] = case

    both_completed = codex_only_completed = ag_only_completed = neither_completed = 0
    both_quality = codex_only_quality = ag_only_quality = neither_quality = 0
    identity_pairs = 0

    for pair_cases in pairs.values():
        c_codex = pair_cases["codex"]
        c_ag = pair_cases["antigravity"]

        if c_codex.accepted and c_ag.accepted:
            both_completed += 1
        elif c_codex.accepted and not c_ag.accepted:
            codex_only_completed += 1
        elif c_ag.accepted and not c_codex.accepted:
            ag_only_completed += 1
        else:
            neither_completed += 1

        codex_passed = c_codex.quality_status == "pass"
        ag_passed = c_ag.quality_status == "pass"
        if codex_passed and ag_passed:
            both_quality += 1
        elif codex_passed and not ag_passed:
            codex_only_quality += 1
        elif ag_passed and not codex_passed:
            ag_only_quality += 1
        else:
            neither_quality += 1

        if c_codex.identity_matches and c_ag.identity_matches:
            identity_pairs += 1

    return M29MutationPairedTaskSummary(
        pair_count=len(pairs),
        both_completed_count=both_completed,
        codex_only_completed_count=codex_only_completed,
        antigravity_only_completed_count=ag_only_completed,
        neither_completed_count=neither_completed,
        both_quality_pass_count=both_quality,
        codex_only_quality_pass_count=codex_only_quality,
        antigravity_only_quality_pass_count=ag_only_quality,
        neither_quality_pass_count=neither_quality,
        identity_complete_pair_count=identity_pairs,
    )


class M29MutationPairedAnalysisReport(StrictModel):
    """Sanitized public paired supplement for the feature/mutation wave."""

    schema_version: Literal[1] = 1
    suite_name: Literal[M29_MUTATION_SUITE_NAME] = M29_MUTATION_SUITE_NAME
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    as_of: datetime
    paired_summary: M29MutationPairedTaskSummary


def build_mutation_paired_report(
    manifest: M29MutationManifest,
    *,
    manifest_sha256_hash: str,
) -> M29MutationPairedAnalysisReport:
    """Build the public paired supplement bound to the mutation manifest."""
    return M29MutationPairedAnalysisReport(
        suite_name=manifest.suite_name,
        suite_sha256=manifest.suite_sha256,
        build_sha=manifest.build_sha,
        manifest_sha256=manifest_sha256_hash,
        as_of=manifest.as_of,
        paired_summary=analyze_mutation_paired_manifest(manifest),
    )


class M29MutationManifest(StrictModel):
    """Sanitized case-level manifest bound to M29 feature/mutation reports."""

    schema_version: Literal[1] = 1
    suite_name: Literal[M29_MUTATION_SUITE_NAME] = M29_MUTATION_SUITE_NAME
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    target_repository_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    as_of: datetime
    advisory_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operational_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    robustness_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cohort_reconciliation: M29MutationCohortReconciliation
    cases: list[M29MutationManifestCase]

    @model_validator(mode="after")
    def validate_manifest(self) -> M29MutationManifest:
        _validate_manifest_structure(self)
        return self


def _validate_manifest_structure(manifest: M29MutationManifest) -> None:
    if len(manifest.cases) != 20:
        raise ValueError(f"manifest must contain exactly 20 cases, got {len(manifest.cases)}")
    if len({c.case_id for c in manifest.cases}) != len(manifest.cases):
        raise ValueError("duplicate case IDs found in manifest")

    for provider in ("codex", "antigravity"):
        p_cases = [c for c in manifest.cases if c.provider == provider]
        if len(p_cases) != 10:
            raise ValueError(f"manifest must contain 10 {provider} cases, got {len(p_cases)}")

    _validate_pairs_and_alternation(manifest.cases)
    _validate_cohort_reconciliation_binding(manifest)


def _validate_pairs_and_alternation(cases: list[M29MutationManifestCase]) -> None:
    for i in range(10):
        c1, c2 = cases[2 * i], cases[2 * i + 1]
        if c1.pair_group != c2.pair_group:
            raise ValueError(
                f"cases at indices {2 * i} and {2 * i + 1} must share pair_group "
                f"('{c1.pair_group}' != '{c2.pair_group}')"
            )
        if (c1.pair_order, c2.pair_order) != (1, 2):
            raise ValueError(
                f"pair '{c1.pair_group}' cases must have pair_order (1, 2), "
                f"got ({c1.pair_order}, {c2.pair_order})"
            )
        if {c1.provider, c2.provider} != {"codex", "antigravity"}:
            raise ValueError(f"pair '{c1.pair_group}' must contain one Codex and one Antigravity")
        expected_first = "codex" if i % 2 == 0 else "antigravity"
        if c1.provider != expected_first:
            raise ValueError(
                f"pair index {i + 1} ('{c1.pair_group}') must start with {expected_first}, "
                f"got {c1.provider}"
            )


def _validate_cohort_reconciliation_binding(manifest: M29MutationManifest) -> None:
    recon = manifest.cohort_reconciliation
    if manifest.as_of != recon.as_of:
        raise ValueError(
            f"manifest as_of ({manifest.as_of.isoformat()}) does not match cohort "
            f"reconciliation as_of ({recon.as_of.isoformat()})"
        )
    if not recon.report_snapshot_verified:
        raise ValueError("manifest cohort reconciliation report_snapshot_verified must be True")

    for profile in M29_MUTATION_PROFILES:
        p_cases = [c for c in manifest.cases if c.worker_profile == profile]
        p_recon = recon.profiles.get(profile)
        if p_recon is None:
            raise ValueError(f"cohort reconciliation is missing profile '{profile}'")
        if p_recon.scheduled_suite_count != 10:
            raise ValueError(
                f"profile '{profile}' scheduled_suite_count must be 10, "
                f"got {p_recon.scheduled_suite_count}"
            )
        eligible_count = sum(c.exclusion_reason is None for c in p_cases)
        excluded_count = sum(c.exclusion_reason is not None for c in p_cases)
        if p_recon.eligible_suite_count != eligible_count:
            raise ValueError(
                f"profile '{profile}' eligible_suite_count mismatch: "
                f"reconciliation has {p_recon.eligible_suite_count}, manifest has {eligible_count}"
            )
        if p_recon.excluded_suite_count != excluded_count:
            raise ValueError(
                f"profile '{profile}' excluded_suite_count mismatch: "
                f"reconciliation has {p_recon.excluded_suite_count}, manifest has {excluded_count}"
            )


MUTATION_MANIFEST_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "suite_name",
        "suite_sha256",
        "build_sha",
        "target_repository_revision",
        "as_of",
        "advisory_report_sha256",
        "operational_report_sha256",
        "robustness_report_sha256",
        "cohort_reconciliation",
        "cases",
    }
)

MUTATION_CASE_ALLOWED_KEYS: frozenset[str] = frozenset(M29MutationManifestCase.model_fields.keys())

COHORT_RECON_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "as_of",
        "window_start_at",
        "window_end_at",
        "report_snapshot_verified",
        "profiles",
    }
)

PROFILE_RECON_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "baseline_count",
        "scheduled_suite_count",
        "eligible_suite_count",
        "excluded_suite_count",
        "current_count",
        "baseline_ids_sha256",
        "scheduled_suite_ids_sha256",
        "eligible_suite_ids_sha256",
        "excluded_suite_ids_sha256",
        "current_ids_sha256",
        "reconciliation_status",
    }
)

PAIRED_REPORT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "suite_name",
        "suite_sha256",
        "build_sha",
        "manifest_sha256",
        "as_of",
        "paired_summary",
    }
)

FORBIDDEN_PRIVATE_KEYS: frozenset[str] = frozenset(
    {
        "task_id",
        "task_text",
        "repo_url",
        "branch",
        "secrets",
        "summary",
        "logs",
        "diff",
        "raw_diff",
        "diff_text",
        "patch",
        "token",
        "password",
        "api_key",
    }
)


def _scan_public_values(value: object, path: str = "root") -> None:
    """Scan nested manifest values for private-content markers."""
    if isinstance(value, str):
        if any(fragment in value for fragment in UNSAFE_VALUE_SUBSTRINGS):
            raise ValueError(f"unsafe value detected at {path}")
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_PRIVATE_KEYS:
                raise ValueError(f"private key detected in manifest: {key}")
            _scan_public_values(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_public_values(child, f"{path}[{index}]")


def assert_sanitized_mutation_manifest(payload: dict[str, Any] | str) -> None:
    """Reject private identifiers, paths, URLs, and undeclared manifest keys."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    if set(data) != MUTATION_MANIFEST_ALLOWED_KEYS:
        extra = sorted(set(data) - MUTATION_MANIFEST_ALLOWED_KEYS)
        missing = sorted(MUTATION_MANIFEST_ALLOWED_KEYS - set(data))
        raise ValueError(f"invalid manifest keys (extra={extra}, missing={missing})")

    for case in data.get("cases", []):
        if set(case) != MUTATION_CASE_ALLOWED_KEYS:
            extra = sorted(set(case) - MUTATION_CASE_ALLOWED_KEYS)
            raise ValueError(f"invalid case keys: {extra}")

    recon = data.get("cohort_reconciliation", {})
    if set(recon) != COHORT_RECON_ALLOWED_KEYS:
        extra = sorted(set(recon) - COHORT_RECON_ALLOWED_KEYS)
        raise ValueError(f"invalid cohort reconciliation keys: {extra}")

    for profile, p_data in recon.get("profiles", {}).items():
        if set(p_data) != PROFILE_RECON_ALLOWED_KEYS:
            extra = sorted(set(p_data) - PROFILE_RECON_ALLOWED_KEYS)
            raise ValueError(f"invalid profile reconciliation keys for {profile}: {extra}")

    M29MutationManifest.model_validate(data)
    _scan_public_values(data)


def assert_sanitized_mutation_paired_report(payload: dict[str, Any] | str) -> None:
    """Reject private fields from the public mutation paired analysis supplement."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    if set(data) != PAIRED_REPORT_ALLOWED_KEYS:
        extra = sorted(set(data) - PAIRED_REPORT_ALLOWED_KEYS)
        raise ValueError(f"invalid paired report keys: {extra}")
    M29MutationPairedAnalysisReport.model_validate(data)
    _scan_public_values(data)


def manifest_sha256(manifest: M29MutationManifest) -> str:
    """Hash the canonical sanitized manifest JSON."""
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "M29MutationManifest",
    "M29MutationManifestCase",
    "M29MutationPairedAnalysisReport",
    "M29MutationPairedTaskSummary",
    "analyze_mutation_paired_manifest",
    "assert_sanitized_mutation_manifest",
    "assert_sanitized_mutation_paired_report",
    "build_mutation_paired_report",
    "manifest_sha256",
]
