#!/usr/bin/env python3
"""Build a sanitized case-level manifest for the private Wave 4 bundle."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from db.models import Task
from evaluation.m29_evidence_models import (
    M29CaseOutcome,
    M29EvidenceBundle,
    M29EvidenceCase,
    M29EvidenceSuite,
)
from evaluation.m29_wave3_paired import Wave3Manifest
from evaluation.m29_wave4_paired import (
    Wave4Manifest,
    Wave4ManifestCase,
    Wave4SupplementalObservation,
    assert_sanitized_wave4_manifest,
)
from evaluation.m29_wave4_reconciliation import assert_wave4_manifest_matches_report
from evaluation.provider_reliability_extractor import (
    ExtractedTaskEvidence,
    build_evidence_cells,
    load_and_classify_tasks,
)
from evaluation.provider_reliability_models import (
    M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES,
    ProviderReliabilityEvidenceCell,
    ProviderReliabilityReport,
    ReliabilityReportPolicy,
)
from evaluation.provider_reliability_stages import resolve_failure_kind
from repositories import create_engine_from_url
from scripts.e2e.build_m29_wave3_manifest import (
    _load_tasks,
    _manifest_case,
    _parse_as_of,
    _sha256,
    _validate_suite_digest,
)

LOGGER = logging.getLogger("build_m29_wave4_manifest")


@dataclass(frozen=True)
class _Wave4Snapshot:
    cases: list[Wave4ManifestCase]
    extractor_cells: list[ProviderReliabilityEvidenceCell]
    baseline_task_ids_by_provider: dict[str, set[str]]
    cell_extractor_task_ids_by_provider: dict[str, set[str]]
    eligible_wave4_task_ids_by_provider: dict[str, set[str]]
    supplemental_observations: list[Wave4SupplementalObservation]
    supplemental_task_ids_by_provider: dict[str, set[str]]
    extractor_task_ids: set[str]


def _baseline_task_ids_by_provider(
    baseline_manifest: Wave3Manifest, baseline_bundle: M29EvidenceBundle
) -> dict[str, set[str]]:
    """Resolve the task-id-private Wave 3 cohort represented by the baseline manifest."""
    if baseline_bundle.suite_sha256 != baseline_manifest.suite_sha256:
        raise ValueError("Wave 3 baseline bundle suite hash does not match its manifest")
    if baseline_bundle.identity.build_sha != baseline_manifest.build_sha:
        raise ValueError("Wave 3 baseline bundle build does not match its manifest")
    if (
        baseline_bundle.identity.target_repository_revision
        != baseline_manifest.target_repository_revision
    ):
        raise ValueError("Wave 3 baseline bundle target revision does not match its manifest")
    if set(baseline_bundle.cases) != {case.case_id for case in baseline_manifest.cases}:
        raise ValueError("Wave 3 baseline bundle cases do not match its manifest")

    result = {provider: set() for provider in ("codex", "antigravity")}
    for case in baseline_manifest.cases:
        if (
            case.task_class == "investigation"
            and case.worker_profile.endswith("-native-executor-read-only")
            and case.identity_matches
            and case.exclusion_reason is None
        ):
            outcome = baseline_bundle.cases[case.case_id]
            if (
                outcome.task_class != case.task_class
                or outcome.worker_profile != case.worker_profile
            ):
                raise ValueError("Wave 3 baseline outcome does not match its sanitized case")
            result[case.provider].add(outcome.task_id)
    return result


def _validate_related_bundle(
    related: M29EvidenceBundle, canonical: M29EvidenceBundle, suite_sha256: str
) -> None:
    """Require a private prior bundle to belong to the same frozen Wave 4 run."""
    if related.suite_sha256 != suite_sha256:
        raise ValueError("prior Wave 4 bundle suite hash does not match the frozen suite")
    if (
        related.identity.build_sha != canonical.identity.build_sha
        or related.identity.target_repository_revision
        != canonical.identity.target_repository_revision
    ):
        raise ValueError("prior Wave 4 bundle build or target revision does not match")


def _manifest_cases(
    bundle: M29EvidenceBundle,
    suite_by_id: dict[str, M29EvidenceCase],
    tasks: dict[str, Task],
    extracted_by_id: dict[str, ExtractedTaskEvidence],
    policy: ReliabilityReportPolicy,
) -> list[Wave4ManifestCase]:
    """Project final-bundle outcomes into sanitized manifest cases."""
    cases = []
    for case_id, outcome in sorted(bundle.cases.items()):
        case = suite_by_id[case_id]
        task = tasks[outcome.task_id]
        manifest_case = _manifest_case(case, outcome, task, policy)
        extracted = extracted_by_id.get(outcome.task_id)
        report_failure_kind = extracted.failure_kind if extracted else None
        if outcome.terminal_status == "failed" and report_failure_kind is None:
            runs = sorted(
                task.worker_runs,
                key=lambda run: (run.started_at or policy.as_of, str(run.id)),
            )
            report_failure_kind = resolve_failure_kind(task, False, runs)
        cases.append(
            Wave4ManifestCase.model_validate(
                {
                    **manifest_case.model_dump(mode="python"),
                    "report_failure_kind": report_failure_kind,
                }
            )
        )
    return cases


def _provider_task_sets(
    bundle: M29EvidenceBundle,
    cases: list[Wave4ManifestCase],
    extracted_tasks: list[ExtractedTaskEvidence],
) -> tuple[dict[str, set[str]], dict[str, set[str]], set[str]]:
    """Capture eligible Wave 4 and canonical cell task IDs privately."""
    wave4_ids = {
        provider: {
            bundle.cases[case.case_id].task_id
            for case in cases
            if case.provider == provider and case.exclusion_reason is None
        }
        for provider in ("codex", "antigravity")
    }
    cell_ids = {
        provider: {
            task.task_id
            for task in extracted_tasks
            if task.task_id is not None
            and task.task_class == "investigation"
            and task.profile == f"{provider}-native-executor-read-only"
            and task.mutation_mode == "read_only"
        }
        for provider in ("codex", "antigravity")
    }
    all_ids = {task.task_id for task in extracted_tasks if task.task_id is not None}
    return wave4_ids, cell_ids, all_ids


def _supplemental_observations(
    prior_bundles: list[M29EvidenceBundle],
    bundle: M29EvidenceBundle,
    suite_by_id: dict[str, M29EvidenceCase],
    tasks: dict[str, Task],
    extracted_by_id: dict[str, ExtractedTaskEvidence],
    cell_ids: dict[str, set[str]],
    policy: ReliabilityReportPolicy,
) -> tuple[list[Wave4SupplementalObservation], dict[str, set[str]]]:
    """Record eligible earlier attempts as cumulative-only sanitized observations."""
    observations = []
    task_ids = {provider: set() for provider in ("codex", "antigravity")}
    final_ids_by_case = {case_id: outcome.task_id for case_id, outcome in bundle.cases.items()}
    seen_ids: set[str] = set()
    for prior in prior_bundles:
        for case_id, outcome in sorted(prior.cases.items()):
            case = suite_by_id.get(case_id)
            if case is None:
                raise ValueError("prior Wave 4 bundle contains a case outside the frozen suite")
            if outcome.task_id == final_ids_by_case.get(case_id):
                continue
            provider = "codex" if case.worker_profile.startswith("codex-") else "antigravity"
            if outcome.task_id not in cell_ids[provider] or outcome.task_id in seen_ids:
                continue
            observations.append(
                _supplemental_observation(
                    case, outcome, tasks[outcome.task_id], extracted_by_id, policy
                )
            )
            task_ids[provider].add(outcome.task_id)
            seen_ids.add(outcome.task_id)
    return observations, task_ids


def _supplemental_observation(
    case: M29EvidenceCase,
    outcome: M29CaseOutcome,
    task: Task,
    extracted_by_id: dict[str, ExtractedTaskEvidence],
    policy: ReliabilityReportPolicy,
) -> Wave4SupplementalObservation:
    """Sanitize one earlier task after its canonical extractor eligibility is proven."""
    prior_case = _manifest_case(case, outcome, task, policy)
    extracted = extracted_by_id[outcome.task_id]
    return Wave4SupplementalObservation(
        case_id=prior_case.case_id,
        task_class=prior_case.task_class,
        pair_group=prior_case.pair_group,
        provider=prior_case.provider,
        worker_profile=prior_case.worker_profile,
        terminal_status=prior_case.terminal_status,
        accepted=prior_case.accepted,
        failure_kind=prior_case.failure_kind,
        report_failure_kind=extracted.failure_kind,
        time_to_terminal_seconds=prior_case.time_to_terminal_seconds,
        execution_identity_status=prior_case.execution_identity_status,
        identity_matches=prior_case.identity_matches,
    )


def _load_wave4_cases_and_snapshot(
    session: Session,
    bundle: M29EvidenceBundle,
    prior_bundles: list[M29EvidenceBundle],
    baseline_task_ids_by_provider: dict[str, set[str]],
    suite: M29EvidenceSuite,
    policy: ReliabilityReportPolicy,
) -> _Wave4Snapshot:
    """Reconcile bundle cases and capture the extractor's private snapshot."""
    task_ids = list(
        {
            outcome.task_id
            for source in [bundle, *prior_bundles]
            for outcome in source.cases.values()
        }
    )
    tasks = _load_tasks(session, task_ids)
    suite_by_id = {case.case_id: case for case in suite.cases}
    extracted_tasks, _ = load_and_classify_tasks(session, policy)
    extracted_by_id = {item.task_id: item for item in extracted_tasks if item.task_id is not None}
    cases = _manifest_cases(bundle, suite_by_id, tasks, extracted_by_id, policy)
    extractor_cells = build_evidence_cells(extracted_tasks, policy)
    wave4_ids, cell_ids, all_ids = _provider_task_sets(bundle, cases, extracted_tasks)
    supplemental, supplemental_ids = _supplemental_observations(
        prior_bundles, bundle, suite_by_id, tasks, extracted_by_id, cell_ids, policy
    )
    return _Wave4Snapshot(
        cases=cases,
        extractor_cells=extractor_cells,
        baseline_task_ids_by_provider=baseline_task_ids_by_provider,
        cell_extractor_task_ids_by_provider=cell_ids,
        eligible_wave4_task_ids_by_provider=wave4_ids,
        supplemental_observations=supplemental,
        supplemental_task_ids_by_provider=supplemental_ids,
        extractor_task_ids=all_ids,
    )


def _load_evidence_inputs(
    args: argparse.Namespace,
) -> tuple[
    M29EvidenceSuite,
    M29EvidenceBundle,
    str,
    dict[str, set[str]],
    list[M29EvidenceBundle],
]:
    """Load and cross-check frozen suite, bundle, baseline, and prior attempts."""
    suite = M29EvidenceSuite.model_validate(json.loads(args.suite.read_text(encoding="utf-8")))
    if suite.suite_name != "m29-live-provider-evidence-wave4":
        raise ValueError("Wave 4 manifest builder requires the Wave 4 suite")
    bundle = M29EvidenceBundle.model_validate(
        json.loads((args.bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    )
    suite_sha256 = _validate_suite_digest(bundle, args.suite)
    if set(bundle.cases) != {case.case_id for case in suite.cases}:
        raise ValueError("bundle cases do not exactly match the frozen Wave 4 suite")

    baseline_manifest = Wave3Manifest.model_validate(
        json.loads(args.baseline_manifest.read_text(encoding="utf-8"))
    )
    baseline_bundle = M29EvidenceBundle.model_validate(
        json.loads((args.baseline_bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    )
    if _sha256(args.baseline_report) != baseline_manifest.advisory_report_sha256:
        raise ValueError("Wave 3 baseline report hash does not match its manifest")
    baseline_ids = _baseline_task_ids_by_provider(baseline_manifest, baseline_bundle)
    prior_bundles = [
        M29EvidenceBundle.model_validate(
            json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
        )
        for bundle_dir in getattr(args, "prior_bundle_dirs", [])
    ]
    for prior_bundle in prior_bundles:
        _validate_related_bundle(prior_bundle, bundle, suite_sha256)
        if not set(prior_bundle.cases).issubset({case.case_id for case in suite.cases}):
            raise ValueError("prior Wave 4 bundle contains cases outside the frozen suite")
    return suite, bundle, suite_sha256, baseline_ids, prior_bundles


def build_manifest(args: argparse.Namespace) -> Wave4Manifest:
    """Build and validate the sanitized manifest from bundle and PostgreSQL state."""
    suite, bundle, suite_sha256, baseline_ids, prior_bundles = _load_evidence_inputs(args)

    as_of = _parse_as_of(args.as_of)
    policy = ReliabilityReportPolicy(
        as_of=as_of,
        window_start_at=as_of - timedelta(days=90),
        window_end_at=as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_WAVE3_WAVE4_FROZEN_EXPECTED_EXECUTION_IDENTITIES),
    )
    database_url = os.getenv(args.database_url_env)
    if not database_url:
        raise ValueError(f"database URL environment variable is unset: {args.database_url_env}")
    engine = create_engine_from_url(database_url)
    snapshot: _Wave4Snapshot | None = None
    try:
        with Session(engine) as session:
            if engine.dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION READ ONLY"))
            snapshot = _load_wave4_cases_and_snapshot(
                session,
                bundle,
                prior_bundles,
                baseline_ids,
                suite,
                policy,
            )
    finally:
        engine.dispose()

    if snapshot is None:
        raise RuntimeError("Wave 4 extractor snapshot was not captured")
    manifest = Wave4Manifest(
        suite_name=suite.suite_name,
        suite_sha256=suite_sha256,
        build_sha=bundle.identity.build_sha,
        target_repository_revision=bundle.identity.target_repository_revision,
        as_of=as_of,
        baseline_report_sha256=_sha256(args.baseline_report),
        advisory_report_sha256=_sha256(args.advisory_report),
        operational_report_sha256=_sha256(args.operational_report),
        robustness_report_sha256=_sha256(args.robustness_report),
        cases=snapshot.cases,
        supplemental_observations=snapshot.supplemental_observations,
    )
    assert_sanitized_wave4_manifest(manifest.model_dump(mode="json"))
    report = ProviderReliabilityReport.model_validate(
        json.loads(args.advisory_report.read_text(encoding="utf-8"))
    )
    baseline_report = ProviderReliabilityReport.model_validate(
        json.loads(args.baseline_report.read_text(encoding="utf-8"))
    )
    assert_wave4_manifest_matches_report(
        manifest,
        report,
        baseline_report=baseline_report,
        extractor_cells=snapshot.extractor_cells,
        baseline_task_ids_by_provider=snapshot.baseline_task_ids_by_provider,
        cell_extractor_task_ids_by_provider=snapshot.cell_extractor_task_ids_by_provider,
        supplemental_task_ids_by_provider=snapshot.supplemental_task_ids_by_provider,
        wave4_task_ids_by_provider=snapshot.eligible_wave4_task_ids_by_provider,
        extractor_task_ids=snapshot.extractor_task_ids,
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--database-url-env", required=True)
    parser.add_argument("--advisory-report", type=Path, required=True)
    parser.add_argument("--operational-report", type=Path, required=True)
    parser.add_argument("--robustness-report", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--baseline-report", type=Path, required=True)
    parser.add_argument("--baseline-manifest", type=Path, required=True)
    parser.add_argument("--baseline-bundle-dir", type=Path, required=True)
    parser.add_argument(
        "--prior-bundle-dir", dest="prior_bundle_dirs", type=Path, action="append", default=[]
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Build a public manifest while keeping raw bundle data private."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    try:
        manifest = build_manifest(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        LOGGER.error("Failed to build sanitized Wave 4 manifest: %s", exc)
        return 1
    print(f"Wrote sanitized Wave 4 manifest to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
