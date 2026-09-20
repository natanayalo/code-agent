#!/usr/bin/env python3
"""Build a sanitized case-level manifest for the private Wave 4 bundle."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from evaluation.m29_evidence_models import M29EvidenceBundle, M29EvidenceSuite
from evaluation.m29_wave4_paired import (
    Wave4Manifest,
    Wave4ManifestCase,
    assert_sanitized_wave4_manifest,
    assert_wave4_manifest_matches_report,
)
from evaluation.provider_reliability_models import (
    ProviderReliabilityReport,
    ReliabilityReportPolicy,
)
from repositories import create_engine_from_url
from scripts.e2e.build_m29_wave3_manifest import (
    _load_tasks,
    _manifest_case,
    _parse_as_of,
    _sha256,
    _validate_suite_digest,
)

LOGGER = logging.getLogger("build_m29_wave4_manifest")


def build_manifest(args: argparse.Namespace) -> Wave4Manifest:
    """Build and validate the sanitized manifest from bundle and PostgreSQL state."""
    suite = M29EvidenceSuite.model_validate(json.loads(args.suite.read_text(encoding="utf-8")))
    if suite.suite_name != "m29-live-provider-evidence-wave4":
        raise ValueError("Wave 4 manifest builder requires the Wave 4 suite")
    bundle = M29EvidenceBundle.model_validate(
        json.loads((args.bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    )
    suite_sha256 = _validate_suite_digest(bundle, args.suite)
    if set(bundle.cases) != {case.case_id for case in suite.cases}:
        raise ValueError("bundle cases do not exactly match the frozen Wave 4 suite")

    as_of = _parse_as_of(args.as_of)
    policy = ReliabilityReportPolicy(
        as_of=as_of,
        window_start_at=as_of - timedelta(days=90),
        window_end_at=as_of,
        min_samples=10,
        evidence_scope="current_execution_cohort",
    )
    database_url = os.getenv(args.database_url_env)
    if not database_url:
        raise ValueError(f"database URL environment variable is unset: {args.database_url_env}")
    engine = create_engine_from_url(database_url)
    try:
        with Session(engine) as session:
            if engine.dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION READ ONLY"))
            task_ids = [outcome.task_id for outcome in bundle.cases.values()]
            if len(task_ids) != len(set(task_ids)):
                raise ValueError("bundle task IDs must be unique across cases")
            tasks = _load_tasks(session, task_ids)
            suite_by_id = {case.case_id: case for case in suite.cases}
            cases = [
                Wave4ManifestCase.model_validate(
                    _manifest_case(
                        suite_by_id[case_id],
                        outcome,
                        tasks[outcome.task_id],
                        policy,
                    ).model_dump(mode="python")
                )
                for case_id, outcome in sorted(bundle.cases.items())
            ]
    finally:
        engine.dispose()

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
        cases=cases,
    )
    assert_sanitized_wave4_manifest(manifest.model_dump(mode="json"))
    report = ProviderReliabilityReport.model_validate(
        json.loads(args.advisory_report.read_text(encoding="utf-8"))
    )
    baseline_report = ProviderReliabilityReport.model_validate(
        json.loads(args.baseline_report.read_text(encoding="utf-8"))
    )
    assert_wave4_manifest_matches_report(manifest, report, baseline_report=baseline_report)
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
