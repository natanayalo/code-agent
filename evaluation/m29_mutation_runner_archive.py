"""Sanitized archive validation and publication reconciliation for M29 mutation runner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from evaluation.m29_mutation_manifest import (
    UNSAFE_VALUE_SUBSTRINGS,
    M29MutationManifest,
    M29MutationManifestCase,
)
from evaluation.m29_mutation_reconciliation import assert_mutation_manifest_publication_ready
from evaluation.m29_mutation_runner_models import (
    ExternalEvaluationResult,
    SanitizedCaseManifestEntry,
)
from evaluation.m29_mutation_suite import M29MutationCase, M29MutationSuite
from evaluation.provider_reliability_models import (
    ProviderReliabilityReport,
    ProviderReliabilityRobustnessReport,
)


def build_sanitized_case_entry(
    *,
    case: M29MutationCase,
    task_status: str,
    failure_kind: str | None = None,
    time_to_terminal_seconds: float | None = None,
    candidate_identity_status: str = "verified",
    identity_matches: bool = True,
    exclusion_reason: str | None = None,
    eval_result: ExternalEvaluationResult | None = None,
) -> SanitizedCaseManifestEntry:
    """Construct validated public-safe SanitizedCaseManifestEntry."""
    provider: Literal["codex", "antigravity"] = (
        "codex" if case.worker_profile == "codex-native-executor" else "antigravity"
    )
    accepted = (task_status == "completed") and (exclusion_reason is None)
    rep_failure_kind = (
        (failure_kind or "unknown")
        if (task_status == "failed" and exclusion_reason is None)
        else None
    )

    if (
        eval_result is None
        or not eval_result.evaluator_identity_matches
        or eval_result.evaluator_identity_status != "verified"
    ):
        q_status: Literal["pass", "fail", "not_assessable"] = "not_assessable"
        if eval_result is None:
            eval_id_status: Literal["verified", "mismatched", "unavailable"] = "unavailable"
        elif eval_result.evaluator_identity_status in ("mismatched", "unavailable"):
            eval_id_status = eval_result.evaluator_identity_status
        elif not eval_result.evaluator_identity_matches:
            eval_id_status = "mismatched"
        else:
            eval_id_status = "unavailable"
        eval_id_matches = False
        o_pass, r_pass, rev_pass = False, False, False
        q_reason = (
            eval_result.reason
            if eval_result and eval_result.reason
            else "evaluator_unavailable_or_unverified"
        )
    else:
        q_status = eval_result.quality_status  # type: ignore[assignment]
        eval_id_status = "verified"
        eval_id_matches = True
        o_pass = eval_result.acceptance_oracle_passed
        r_pass = eval_result.regression_suite_passed
        rev_pass = eval_result.neutral_review_passed
        q_reason = eval_result.reason

    return SanitizedCaseManifestEntry(
        case_id=case.case_id,
        pair_group=case.pair_group,
        pair_order=case.pair_order,
        topic=case.topic,
        task_class="feature",
        mutation_mode="mutation",
        worker_profile=case.worker_profile,
        provider=provider,
        terminal_status="completed" if task_status == "completed" else "failed",
        accepted=accepted,
        failure_kind=failure_kind,
        report_failure_kind=rep_failure_kind,
        time_to_terminal_seconds=time_to_terminal_seconds,
        execution_identity_status=candidate_identity_status,  # type: ignore[arg-type]
        identity_matches=identity_matches,
        exclusion_reason=exclusion_reason,
        quality_status=q_status,
        acceptance_oracle_passed=o_pass,
        regression_suite_passed=r_pass,
        neutral_review_passed=rev_pass,
        evaluator_identity_status=eval_id_status,
        evaluator_identity_matches=eval_id_matches,
        quality_reason=q_reason,
    )


def validate_sanitized_archive(
    sanitized_cases: Sequence[SanitizedCaseManifestEntry | M29MutationManifestCase],
    *,
    sanitized_diffs: Mapping[str, str] | None = None,
    expected_case_count: int = 20,
) -> str:
    """Validate sanitized archive invariants, check for credentials, and return SHA-256."""
    if len(sanitized_cases) != expected_case_count:
        raise ValueError(
            f"sanitized archive case count mismatch: expected {expected_case_count}, "
            f"got {len(sanitized_cases)}"
        )

    case_dicts: list[dict[str, Any]] = []
    for c in sanitized_cases:
        d = c.model_dump(mode="json")
        for key in ("task_id", "workspace_id", "repo_url", "diff", "raw_diff", "logs"):
            if key in d:
                raise ValueError(f"private key '{key}' found in sanitized case {c.case_id}")
        case_dicts.append(d)

    if sanitized_diffs is not None:
        case_ids = {c.case_id for c in sanitized_cases}
        for cid, diff in sanitized_diffs.items():
            if cid not in case_ids:
                raise ValueError(f"diff case_id '{cid}' not found in sanitized cases")
            for unsafe in UNSAFE_VALUE_SUBSTRINGS:
                if unsafe in diff:
                    raise ValueError(f"unsafe credential substring in diff for {cid}: {unsafe}")

    payload = {"case_count": len(case_dicts), "cases": case_dicts}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


validate_sanitized_Archive = validate_sanitized_archive


def reconcile_m29_publication_gate(
    manifest: M29MutationManifest,
    suite: M29MutationSuite,
    canonical_report: ProviderReliabilityReport,
    robustness_report: ProviderReliabilityRobustnessReport,
    *,
    baseline_report: ProviderReliabilityReport | None = None,
    canonical_report_json: str | None = None,
    robustness_report_json: str | None = None,
    operational_report_json: str | None = None,
) -> bool:
    """Execute publication gate assertion verifying manifest, reports, and quality separation."""
    assert_mutation_manifest_publication_ready(
        manifest=manifest,
        suite=suite,
        canonical_report=canonical_report,
        robustness_report=robustness_report,
        baseline_report=baseline_report,
        canonical_report_json=canonical_report_json,
        robustness_report_json=robustness_report_json,
        operational_report_json=operational_report_json,
    )
    return True


__all__ = [
    "build_sanitized_case_entry",
    "reconcile_m29_publication_gate",
    "validate_sanitized_Archive",
    "validate_sanitized_archive",
]
