"""Unit coverage for sanitized Wave 3 manifests and paired analysis."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evaluation.m29_wave3_paired import (
    Wave3Manifest,
    Wave3ManifestCase,
    analyze_paired_manifest,
    assert_sanitized_manifest,
)


def _manifest() -> Wave3Manifest:
    """Create a balanced synthetic Wave 3 manifest."""
    cases: list[Wave3ManifestCase] = []
    for task_class in ("investigation", "feature"):
        for index in range(10):
            pair_group = f"m29-w3-{task_class}-{index:02d}"
            for pair_order, provider in enumerate(("codex", "antigravity"), start=1):
                identity_matches = task_class == "feature" or index < 5
                accepted = (
                    index < 5
                    if task_class == "feature"
                    else (provider == "codex" and index % 2 == 0)
                )
                cases.append(
                    Wave3ManifestCase(
                        case_id=f"{pair_group}-{provider}",
                        pair_group=pair_group,
                        pair_order=pair_order,
                        task_class=task_class,
                        worker_profile=f"{provider}-native-executor-read-only",
                        provider=provider,
                        terminal_status="completed" if accepted else "failed",
                        accepted=accepted,
                        failure_kind=None if accepted else "worker_failure",
                        time_to_terminal_seconds=(100.0 if provider == "codex" else 50.0),
                        execution_identity_status=(
                            "verified" if identity_matches else "unknown_legacy"
                        ),
                        identity_matches=identity_matches,
                        exclusion_reason=None if identity_matches else "unknown_execution_identity",
                    )
                )
    return Wave3Manifest(
        suite_name="m29-live-provider-evidence-wave3",
        suite_sha256="a" * 64,
        build_sha="b" * 40,
        target_repository_revision="c" * 40,
        as_of=datetime(2026, 9, 19, tzinfo=UTC),
        advisory_report_sha256="d" * 64,
        operational_report_sha256="e" * 64,
        robustness_report_sha256="f" * 64,
        cases=cases,
    )


def test_manifest_distribution_and_paired_outcomes() -> None:
    """Validate balanced manifest counts and pair-preserving analysis."""
    manifest = _manifest()
    analyses = {item.task_class: item for item in analyze_paired_manifest(manifest, iterations=100)}

    assert analyses["feature"].pair_count == 10
    assert analyses["feature"].identity_complete_pair_count == 10
    assert analyses["feature"].successful_latency_differences.sample_size == 5
    assert analyses["feature"].successful_latency_differences.median_seconds == -50.0
    assert analyses["investigation"].identity_complete_pair_count == 5
    assert analyses["investigation"].bootstrap.identity_complete_pairs == 5


def test_manifest_rejects_duplicate_case_ids() -> None:
    """Reject malformed case distributions before publication."""
    manifest = _manifest()
    duplicate = manifest.cases[-1].model_copy(update={"case_id": manifest.cases[0].case_id})
    with pytest.raises(ValueError, match="duplicate case IDs"):
        Wave3Manifest.model_validate(
            manifest.model_copy(update={"cases": [*manifest.cases[:-1], duplicate]}).model_dump()
        )


def test_manifest_sanitization_rejects_private_keys() -> None:
    """Keep task identifiers and private execution content out of public artifacts."""
    payload = _manifest().model_dump(mode="json")
    payload["cases"][0]["task_id"] = "private"
    with pytest.raises(ValueError):
        assert_sanitized_manifest(payload)
