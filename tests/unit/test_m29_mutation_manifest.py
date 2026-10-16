"""Unit tests for M29 feature/mutation manifest contracts and sanitization."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from evaluation.m29_mutation_cohort import (
    M29MutationCohortReconciliation,
    MutationProfileReconciliation,
)
from evaluation.m29_mutation_manifest import (
    M29MutationManifest,
    M29MutationManifestCase,
    analyze_mutation_paired_manifest,
    assert_sanitized_mutation_manifest,
    assert_sanitized_mutation_paired_report,
    build_mutation_paired_report,
    manifest_sha256,
)
from evaluation.m29_mutation_suite import (
    DEFAULT_FIXTURE_REVISION,
    RAW_PAIR_DEFINITIONS,
    build_m29_mutation_suite,
)


def _make_recon(
    as_of: datetime,
    *,
    codex_eligible: int = 10,
    codex_excluded: int = 0,
    ag_eligible: int = 10,
    ag_excluded: int = 0,
    verified: bool = True,
) -> M29MutationCohortReconciliation:
    return M29MutationCohortReconciliation(
        as_of=as_of,
        window_start_at=as_of - timedelta(days=90),
        window_end_at=as_of,
        report_snapshot_verified=verified,
        profiles={
            "codex-native-executor": MutationProfileReconciliation(
                baseline_count=0,
                scheduled_suite_count=10,
                eligible_suite_count=codex_eligible,
                excluded_suite_count=codex_excluded,
                current_count=codex_eligible,
                baseline_ids_sha256="0" * 64,
                scheduled_suite_ids_sha256="1" * 64,
                eligible_suite_ids_sha256="2" * 64,
                excluded_suite_ids_sha256="3" * 64,
                current_ids_sha256="4" * 64,
            ),
            "antigravity-native-executor": MutationProfileReconciliation(
                baseline_count=0,
                scheduled_suite_count=10,
                eligible_suite_count=ag_eligible,
                excluded_suite_count=ag_excluded,
                current_count=ag_eligible,
                baseline_ids_sha256="5" * 64,
                scheduled_suite_ids_sha256="6" * 64,
                eligible_suite_ids_sha256="7" * 64,
                excluded_suite_ids_sha256="8" * 64,
                current_ids_sha256="9" * 64,
            ),
        },
    )


def _make_valid_manifest(
    as_of: datetime | None = None,
    *,
    suite_digest: str | None = None,
) -> M29MutationManifest:
    suite = build_m29_mutation_suite()
    digest = suite_digest or suite.compute_digest()
    as_of_dt = as_of or datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    cases: list[M29MutationManifestCase] = []

    for idx, raw_def in enumerate(RAW_PAIR_DEFINITIONS, start=1):
        topic, _path, _entry, _prompt, _oracle, slug = raw_def
        pair_group = f"m29-mut-pair-{idx:02d}-{slug}"
        if idx % 2 == 1:
            profiles = (
                ("codex-native-executor", "codex"),
                ("antigravity-native-executor", "antigravity"),
            )
        else:
            profiles = (
                ("antigravity-native-executor", "antigravity"),
                ("codex-native-executor", "codex"),
            )

        for order, (profile, provider) in enumerate(profiles, start=1):
            cases.append(
                M29MutationManifestCase(
                    case_id=f"m29-mut-{idx:02d}-{slug}-{provider}",
                    pair_group=pair_group,
                    pair_order=order,
                    topic=topic,
                    worker_profile=profile,  # type: ignore[arg-type]
                    provider=provider,  # type: ignore[arg-type]
                    terminal_status="completed",
                    accepted=True,
                    failure_kind=None,
                    report_failure_kind=None,
                    time_to_terminal_seconds=120.0,
                    execution_identity_status="verified",
                    identity_matches=True,
                    exclusion_reason=None,
                    quality_status="pass",
                    acceptance_oracle_passed=True,
                    regression_suite_passed=True,
                    neutral_review_passed=True,
                    evaluator_identity_status="verified",
                    evaluator_identity_matches=True,
                    quality_reason=None,
                )
            )

    return M29MutationManifest(
        suite_sha256=digest,
        build_sha="a1b2c3d4e5f67890",
        target_repository_revision=DEFAULT_FIXTURE_REVISION,
        as_of=as_of_dt,
        advisory_report_sha256="a" * 64,
        operational_report_sha256="b" * 64,
        robustness_report_sha256="c" * 64,
        cohort_reconciliation=_make_recon(as_of_dt),
        cases=cases,
    )


def test_valid_manifest_creation_and_digest() -> None:
    manifest = _make_valid_manifest()
    assert len(manifest.cases) == 20
    assert manifest.schema_version == 1
    assert manifest.suite_name == "m29-live-provider-evidence-mutation"
    digest = manifest_sha256(manifest)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_case_provider_profile_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="provider 'codex' does not match"):
        M29MutationManifestCase(
            case_id="m29-mut-01-label-norm-ag",
            pair_group="m29-mut-pair-01-label-norm",
            pair_order=1,
            topic="label_normalization",
            worker_profile="antigravity-native-executor",
            provider="codex",
            terminal_status="completed",
            accepted=True,
            time_to_terminal_seconds=120.0,
            execution_identity_status="verified",
            identity_matches=True,
            quality_status="pass",
            acceptance_oracle_passed=True,
            regression_suite_passed=True,
            neutral_review_passed=True,
            evaluator_identity_status="verified",
            evaluator_identity_matches=True,
        )


def test_case_accepted_invariants_rejected() -> None:
    base: dict[str, Any] = {
        "case_id": "m29-mut-01-label-norm-codex",
        "pair_group": "m29-mut-pair-01-label-norm",
        "pair_order": 1,
        "topic": "label_normalization",
        "worker_profile": "codex-native-executor",
        "provider": "codex",
        "terminal_status": "completed",
        "accepted": True,
        "time_to_terminal_seconds": 120.0,
        "execution_identity_status": "verified",
        "identity_matches": True,
        "quality_status": "pass",
        "acceptance_oracle_passed": True,
        "regression_suite_passed": True,
        "neutral_review_passed": True,
        "evaluator_identity_status": "verified",
        "evaluator_identity_matches": True,
    }

    with pytest.raises(ValidationError, match="accepted cannot be True when terminal_status"):
        M29MutationManifestCase(**{**base, "terminal_status": "failed"})

    with pytest.raises(ValidationError, match="accepted case .* cannot have report_failure_kind"):
        M29MutationManifestCase(**{**base, "report_failure_kind": "timeout"})

    with pytest.raises(ValidationError, match="accepted case .* cannot have exclusion_reason"):
        M29MutationManifestCase(**{**base, "exclusion_reason": "excluded_test"})

    with pytest.raises(ValidationError, match="requires verified matching"):
        M29MutationManifestCase(**{**base, "identity_matches": False})

    with pytest.raises(ValidationError, match="identity_matches cannot be True"):
        M29MutationManifestCase(
            **{**base, "execution_identity_status": "unknown_execution_identity"}
        )


def test_case_failed_invariants() -> None:
    base: dict[str, Any] = {
        "case_id": "m29-mut-01-label-norm-codex",
        "pair_group": "m29-mut-pair-01-label-norm",
        "pair_order": 1,
        "topic": "label_normalization",
        "worker_profile": "codex-native-executor",
        "provider": "codex",
        "terminal_status": "failed",
        "accepted": False,
        "failure_kind": "timeout",
        "report_failure_kind": "timeout",
        "time_to_terminal_seconds": 120.0,
        "execution_identity_status": "verified",
        "identity_matches": True,
        "quality_status": "fail",
        "acceptance_oracle_passed": False,
        "regression_suite_passed": True,
        "neutral_review_passed": True,
        "evaluator_identity_status": "verified",
        "evaluator_identity_matches": True,
        "quality_reason": "timeout",
    }
    case = M29MutationManifestCase(**base)
    assert not case.accepted
    assert case.terminal_status == "failed"

    with pytest.raises(
        ValidationError, match="failed eligible case .* requires report_failure_kind"
    ):
        M29MutationManifestCase(**{**base, "report_failure_kind": None})

    with pytest.raises(ValidationError, match="non-excluded completed case .* must be accepted"):
        M29MutationManifestCase(
            **{**base, "terminal_status": "completed", "report_failure_kind": None}
        )

    # Eligible failed case requires verified matching identity
    with pytest.raises(ValidationError, match="requires verified matching"):
        M29MutationManifestCase(**{**base, "identity_matches": False})

    with pytest.raises(ValidationError, match="requires verified matching"):
        M29MutationManifestCase(
            **{**base, "identity_matches": False, "execution_identity_status": "unknown_legacy"}
        )

    # identity_matches cannot be True when execution_identity_status is not verified
    with pytest.raises(ValidationError, match="identity_matches cannot be True"):
        M29MutationManifestCase(
            **{
                **base,
                "execution_identity_status": "unknown_legacy",
                "identity_matches": True,
                "exclusion_reason": "unverified_legacy",
            }
        )

    # Excluded failed case with unverified identity and identity_matches=False is valid
    excluded_case = M29MutationManifestCase(
        **{
            **base,
            "execution_identity_status": "unknown_legacy",
            "identity_matches": False,
            "exclusion_reason": "unverified_legacy",
        }
    )
    assert excluded_case.exclusion_reason == "unverified_legacy"


def test_case_quality_invariants_rejected() -> None:
    base: dict[str, Any] = {
        "case_id": "m29-mut-01-label-norm-codex",
        "pair_group": "m29-mut-pair-01-label-norm",
        "pair_order": 1,
        "topic": "label_normalization",
        "worker_profile": "codex-native-executor",
        "provider": "codex",
        "terminal_status": "completed",
        "accepted": True,
        "time_to_terminal_seconds": 120.0,
        "execution_identity_status": "verified",
        "identity_matches": True,
        "quality_status": "pass",
        "acceptance_oracle_passed": True,
        "regression_suite_passed": True,
        "neutral_review_passed": True,
        "evaluator_identity_status": "verified",
        "evaluator_identity_matches": True,
    }

    with pytest.raises(ValidationError, match="quality_status cannot be 'pass' when acceptance"):
        M29MutationManifestCase(**{**base, "acceptance_oracle_passed": False})

    with pytest.raises(ValidationError, match="passing quality cannot have quality_reason"):
        M29MutationManifestCase(**{**base, "quality_reason": "unexpected reason"})

    with pytest.raises(ValidationError, match="quality_status must be 'not_assessable'"):
        M29MutationManifestCase(**{**base, "evaluator_identity_matches": False})

    with pytest.raises(
        ValidationError, match="requires quality_reason when quality is not assessable"
    ):
        M29MutationManifestCase(
            **{
                **base,
                "evaluator_identity_matches": False,
                "quality_status": "not_assessable",
                "quality_reason": None,
            }
        )

    with pytest.raises(
        ValidationError, match="quality_status cannot be 'fail' when all checks pass"
    ):
        M29MutationManifestCase(
            **{
                **base,
                "quality_status": "fail",
                "quality_reason": "why fail",
            }
        )

    with pytest.raises(ValidationError, match="failing quality case .* requires quality_reason"):
        M29MutationManifestCase(
            **{
                **base,
                "acceptance_oracle_passed": False,
                "quality_status": "fail",
                "quality_reason": None,
            }
        )


def test_paired_analysis_summary_and_report() -> None:
    manifest = _make_valid_manifest()
    summary = analyze_mutation_paired_manifest(manifest)
    assert summary.pair_count == 10
    assert summary.both_completed_count == 10
    assert summary.codex_only_completed_count == 0
    assert summary.antigravity_only_completed_count == 0
    assert summary.neither_completed_count == 0
    assert summary.both_quality_pass_count == 10
    assert summary.identity_complete_pair_count == 10

    report = build_mutation_paired_report(manifest, manifest_sha256_hash="f" * 64)
    assert report.schema_version == 1
    assert report.manifest_sha256 == "f" * 64
    assert report.paired_summary == summary
    assert_sanitized_mutation_paired_report(report.model_dump(mode="json"))

    # Excluded case does not count toward identity_complete_pair_count
    excluded_cases = list(manifest.cases)
    excluded_cases[0] = excluded_cases[0].model_copy(
        update={
            "accepted": False,
            "exclusion_reason": "test_exclusion",
            "report_failure_kind": "timeout",
            "identity_matches": False,
            "execution_identity_status": "unknown_legacy",
        }
    )
    summary_with_exclusion = analyze_mutation_paired_manifest(
        manifest.model_copy(update={"cases": excluded_cases})
    )
    assert summary_with_exclusion.identity_complete_pair_count == 9


def test_manifest_validation_invariants() -> None:
    manifest = _make_valid_manifest()
    data = manifest.model_dump()

    with pytest.raises(ValidationError, match="manifest must contain exactly 20 cases"):
        M29MutationManifest.model_validate({**data, "cases": manifest.cases[:19]})

    with pytest.raises(ValidationError, match="duplicate case IDs found"):
        dup_cases = list(manifest.cases)
        dup_cases[1] = dup_cases[0]
        M29MutationManifest.model_validate({**data, "cases": dup_cases})

    with pytest.raises(ValidationError, match="must share pair_group"):
        swapped_cases = list(manifest.cases)
        swapped_cases[1], swapped_cases[2] = swapped_cases[2], swapped_cases[1]
        M29MutationManifest.model_validate({**data, "cases": swapped_cases})

    with pytest.raises(ValidationError, match="does not match cohort reconciliation as_of"):
        M29MutationManifest.model_validate(
            {**data, "as_of": manifest.as_of + timedelta(seconds=10)}
        )

    with pytest.raises(ValidationError, match="report_snapshot_verified must be True"):
        unverified_recon = _make_recon(manifest.as_of, verified=False)
        M29MutationManifest.model_validate({**data, "cohort_reconciliation": unverified_recon})

    with pytest.raises(ValidationError, match="eligible_suite_count mismatch"):
        mismatched_recon = _make_recon(manifest.as_of, codex_eligible=9)
        M29MutationManifest.model_validate({**data, "cohort_reconciliation": mismatched_recon})


def test_sanitized_mutation_manifest_passes() -> None:
    manifest = _make_valid_manifest()
    payload = manifest.model_dump(mode="json")
    assert_sanitized_mutation_manifest(payload)
    assert_sanitized_mutation_manifest(manifest.model_dump_json())


def test_sanitized_mutation_manifest_rejects_extra_keys() -> None:
    manifest = _make_valid_manifest()
    payload = manifest.model_dump(mode="json")
    payload["extra_root_field"] = "unexpected"
    with pytest.raises(
        (ValueError, ValidationError),
        match=r"(invalid manifest keys|Extra inputs are not permitted)",
    ):
        assert_sanitized_mutation_manifest(payload)

    payload2 = manifest.model_dump(mode="json")
    payload2["cases"][0]["extra_case_field"] = "unexpected"
    with pytest.raises(
        (ValueError, ValidationError), match=r"(invalid case keys|Extra inputs are not permitted)"
    ):
        assert_sanitized_mutation_manifest(payload2)


def test_sanitized_mutation_manifest_rejects_forbidden_keys() -> None:
    manifest = _make_valid_manifest()
    for forbidden in ("task_id", "task_text", "repo_url", "branch", "diff", "secrets"):
        payload = manifest.model_dump(mode="json")
        payload["cohort_reconciliation"][forbidden] = "leak"
        with pytest.raises(ValueError):
            assert_sanitized_mutation_manifest(payload)


def test_sanitized_mutation_manifest_rejects_unsafe_substrings() -> None:
    manifest = _make_valid_manifest()
    for unsafe in ("https://github.com/repo", "/Users/dev/code-agent", "/tmp/scratch"):
        payload = manifest.model_dump(mode="json")
        payload["cases"][0]["quality_reason"] = f"failed due to {unsafe}"
        # Set quality to fail so quality_reason is allowed schema-wise
        payload["cases"][0]["quality_status"] = "fail"
        payload["cases"][0]["acceptance_oracle_passed"] = False
        with pytest.raises(ValueError, match="unsafe value detected"):
            assert_sanitized_mutation_manifest(payload)


def test_sanitized_paired_report_rejects_extra_keys() -> None:
    manifest = _make_valid_manifest()
    report = build_mutation_paired_report(manifest, manifest_sha256_hash="f" * 64)
    payload = report.model_dump(mode="json")
    payload["extra_key"] = "bad"
    with pytest.raises(
        (ValueError, ValidationError),
        match=r"(invalid paired report keys|Extra inputs are not permitted)",
    ):
        assert_sanitized_mutation_paired_report(payload)


def test_case_quality_not_assessable_with_verified_identity_requires_reason() -> None:
    base: dict[str, Any] = {
        "case_id": "m29-mut-01-label-norm-codex",
        "pair_group": "m29-mut-pair-01-label-norm",
        "pair_order": 1,
        "topic": "label_normalization",
        "worker_profile": "codex-native-executor",
        "provider": "codex",
        "terminal_status": "completed",
        "accepted": True,
        "time_to_terminal_seconds": 120.0,
        "execution_identity_status": "verified",
        "identity_matches": True,
        "acceptance_oracle_passed": False,
        "regression_suite_passed": False,
        "neutral_review_passed": False,
        "evaluator_identity_status": "verified",
        "evaluator_identity_matches": True,
        "quality_status": "not_assessable",
        "quality_reason": None,
    }
    with pytest.raises(
        ValidationError, match="requires quality_reason when quality is not assessable"
    ):
        M29MutationManifestCase(**base)

    valid = M29MutationManifestCase(
        **{**base, "quality_reason": "evaluator infrastructure timeout"}
    )
    assert valid.quality_status == "not_assessable"


def test_paired_analysis_summary_with_mixed_outcomes() -> None:
    manifest = _make_valid_manifest()
    altered_cases = list(manifest.cases)

    # Pair 1 (indices 2, 3): codex only completes, codex only passes quality
    # Index 2 is AG (pair 2 starts with AG), Index 3 is Codex
    altered_cases[2] = altered_cases[2].model_copy(
        update={
            "terminal_status": "failed",
            "accepted": False,
            "report_failure_kind": "timeout",
            "quality_status": "fail",
            "acceptance_oracle_passed": False,
            "quality_reason": "failed",
        }
    )
    # Pair 2 (indices 4, 5): AG only completes, AG only passes quality
    # Index 4 is Codex, Index 5 is AG
    altered_cases[4] = altered_cases[4].model_copy(
        update={
            "terminal_status": "failed",
            "accepted": False,
            "report_failure_kind": "timeout",
            "quality_status": "fail",
            "acceptance_oracle_passed": False,
            "quality_reason": "failed",
        }
    )
    # Pair 3 (indices 6, 7): neither completes, neither passes quality
    for idx in (6, 7):
        altered_cases[idx] = altered_cases[idx].model_copy(
            update={
                "terminal_status": "failed",
                "accepted": False,
                "report_failure_kind": "timeout",
                "quality_status": "fail",
                "acceptance_oracle_passed": False,
                "quality_reason": "failed",
            }
        )

    altered_manifest = manifest.model_copy(update={"cases": altered_cases})
    summary = analyze_mutation_paired_manifest(altered_manifest)
    assert summary.both_completed_count == 7
    assert summary.codex_only_completed_count == 1
    assert summary.antigravity_only_completed_count == 1
    assert summary.neither_completed_count == 1
    assert summary.both_quality_pass_count == 7
    assert summary.codex_only_quality_pass_count == 1
    assert summary.antigravity_only_quality_pass_count == 1
    assert summary.neither_quality_pass_count == 1


def test_manifest_validation_pairing_edge_cases() -> None:
    manifest = _make_valid_manifest()
    data = manifest.model_dump()

    # Provider imbalance (replace an AG case with a Codex case)
    bad_provider_cases = list(manifest.cases)
    bad_provider_cases[1] = bad_provider_cases[0].model_copy(
        update={"case_id": "m29-mut-extra-codex"}
    )
    with pytest.raises(
        ValidationError, match=r"manifest must contain 10 (codex|antigravity) cases"
    ):
        M29MutationManifest.model_validate({**data, "cases": bad_provider_cases})

    # Pair order not (1, 2)
    bad_order_cases = list(manifest.cases)
    bad_order_cases[1] = bad_order_cases[1].model_copy(update={"pair_order": 1})
    with pytest.raises(ValidationError, match="cases must have pair_order"):
        M29MutationManifest.model_validate({**data, "cases": bad_order_cases})

    # Pair contains two cases of same provider
    two_codex_cases = list(manifest.cases)
    two_codex_cases[1] = two_codex_cases[1].model_copy(
        update={"provider": "codex", "worker_profile": "codex-native-executor"}
    )
    two_codex_cases[3] = two_codex_cases[3].model_copy(
        update={"provider": "antigravity", "worker_profile": "antigravity-native-executor"}
    )
    with pytest.raises(ValidationError, match="must contain one Codex and one Antigravity"):
        M29MutationManifest.model_validate({**data, "cases": two_codex_cases})

    # Pair starts with wrong provider
    swapped_pair_start = list(manifest.cases)
    swapped_pair_start[0], swapped_pair_start[1] = (
        swapped_pair_start[1],
        swapped_pair_start[0],
    )
    swapped_pair_start[0] = swapped_pair_start[0].model_copy(update={"pair_order": 1})
    swapped_pair_start[1] = swapped_pair_start[1].model_copy(update={"pair_order": 2})
    with pytest.raises(ValidationError, match="must start with codex"):
        M29MutationManifest.model_validate({**data, "cases": swapped_pair_start})


def test_manifest_validation_reconciliation_profile_and_counts_edge_cases() -> None:
    manifest = _make_valid_manifest()
    data = manifest.model_dump()

    # Cohort reconciliation missing profile
    recon_missing = _make_recon(manifest.as_of)
    bad_profiles = dict(recon_missing.profiles)
    del bad_profiles["codex-native-executor"]
    recon_bad = replace(recon_missing, profiles=bad_profiles)
    with pytest.raises(ValidationError, match="do not match expected profiles"):
        M29MutationManifest.model_validate({**data, "cohort_reconciliation": recon_bad})

    # Scheduled suite count != 10
    recon_bad_sched = _make_recon(manifest.as_of)
    bad_profiles_sched = dict(recon_bad_sched.profiles)
    bad_profiles_sched["codex-native-executor"] = replace(
        bad_profiles_sched["codex-native-executor"], scheduled_suite_count=9
    )
    with pytest.raises(ValidationError, match="scheduled_suite_count must be 10"):
        M29MutationManifest.model_validate(
            {
                **data,
                "cohort_reconciliation": replace(recon_bad_sched, profiles=bad_profiles_sched),
            }
        )

    # Excluded suite count mismatch
    recon_bad_ex = _make_recon(manifest.as_of, codex_excluded=1)
    with pytest.raises(ValidationError, match="excluded_suite_count mismatch"):
        M29MutationManifest.model_validate({**data, "cohort_reconciliation": recon_bad_ex})

    # Reconciliation status not passed
    bad_p_status = dict(recon_missing.profiles)
    bad_p_status["codex-native-executor"] = replace(
        bad_p_status["codex-native-executor"], reconciliation_status="failed"
    )
    with pytest.raises(ValidationError, match="reconciliation_status must be 'passed'"):
        M29MutationManifest.model_validate(
            {**data, "cohort_reconciliation": replace(recon_missing, profiles=bad_p_status)}
        )

    # Baseline count < 0
    bad_p_base = dict(recon_missing.profiles)
    bad_p_base["codex-native-executor"] = replace(
        bad_p_base["codex-native-executor"], baseline_count=-1
    )
    with pytest.raises(ValidationError, match="baseline_count must be non-negative"):
        M29MutationManifest.model_validate(
            {**data, "cohort_reconciliation": replace(recon_missing, profiles=bad_p_base)}
        )

    # Current count mismatch
    bad_p_curr = dict(recon_missing.profiles)
    bad_p_curr["codex-native-executor"] = replace(
        bad_p_curr["codex-native-executor"], current_count=99
    )
    with pytest.raises(ValidationError, match="current_count mismatch"):
        M29MutationManifest.model_validate(
            {**data, "cohort_reconciliation": replace(recon_missing, profiles=bad_p_curr)}
        )


def test_manifest_validation_reconciliation_window_and_digest_edge_cases() -> None:
    manifest = _make_valid_manifest()
    data = manifest.model_dump()
    recon = _make_recon(manifest.as_of)

    # Window end not matching as_of
    recon_bad_end = replace(recon, window_end_at=manifest.as_of + timedelta(seconds=1))
    with pytest.raises(ValidationError, match="window_end_at .* must match as_of"):
        M29MutationManifest.model_validate({**data, "cohort_reconciliation": recon_bad_end})

    # Window start not exactly 90 days before as_of
    recon_bad_start = replace(recon, window_start_at=manifest.as_of - timedelta(days=60))
    with pytest.raises(ValidationError, match="window_start_at .* must be exactly 90 days"):
        M29MutationManifest.model_validate({**data, "cohort_reconciliation": recon_bad_start})

    # Malformed SHA-256 digest in profile reconciliation
    for field_name in (
        "baseline_ids_sha256",
        "scheduled_suite_ids_sha256",
        "eligible_suite_ids_sha256",
        "excluded_suite_ids_sha256",
        "current_ids_sha256",
    ):
        bad_p_sha = dict(recon.profiles)
        bad_p_sha["codex-native-executor"] = replace(
            bad_p_sha["codex-native-executor"], **{field_name: "not-a-sha"}
        )
        with pytest.raises(ValidationError, match="must be a 64-character lowercase hex SHA-256"):
            M29MutationManifest.model_validate(
                {**data, "cohort_reconciliation": replace(recon, profiles=bad_p_sha)}
            )


def test_sanitized_mutation_manifest_missing_or_extra_nested_keys() -> None:
    manifest = _make_valid_manifest()
    payload = manifest.model_dump(mode="json")
    del payload["schema_version"]
    with pytest.raises(ValueError, match="invalid manifest keys"):
        assert_sanitized_mutation_manifest(payload)

    payload2 = manifest.model_dump(mode="json")
    payload2["cohort_reconciliation"]["extra_recon_field"] = "bad"
    with pytest.raises(ValueError, match="invalid cohort reconciliation keys"):
        assert_sanitized_mutation_manifest(payload2)

    payload3 = manifest.model_dump(mode="json")
    payload3["cohort_reconciliation"]["profiles"]["codex-native-executor"][
        "extra_profile_field"
    ] = "bad"
    with pytest.raises(ValueError, match="invalid profile reconciliation keys"):
        assert_sanitized_mutation_manifest(payload3)


def test_scan_public_values_forbidden_private_key() -> None:
    from evaluation.m29_mutation_manifest import _scan_public_values

    with pytest.raises(ValueError, match="private key detected in manifest: summary"):
        _scan_public_values({"summary": "forbidden text"})
