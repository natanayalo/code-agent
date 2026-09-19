#!/usr/bin/env python3
"""Build a sanitized, case-level manifest for the private Wave 3 bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from db.models import Task
from evaluation.m29_evidence_models import M29EvidenceBundle, M29EvidenceSuite
from evaluation.m29_wave3_paired import (
    Wave3Manifest,
    Wave3ManifestCase,
    assert_sanitized_manifest,
)
from evaluation.provider_reliability_extractor import _validate_task_candidate
from evaluation.provider_reliability_identity import (
    check_cohort_membership,
    resolve_task_execution_identity,
)
from evaluation.provider_reliability_models import VALID_FAILURE_KINDS, ReliabilityReportPolicy
from repositories import create_engine_from_url

LOGGER = logging.getLogger("build_m29_wave3_manifest")


def _sha256(path: Path) -> str:
    """Hash a committed report or suite file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_as_of(raw: str) -> datetime:
    """Parse a fixed UTC report timestamp."""
    parsed = datetime.fromisoformat(raw)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _load_tasks(session: Session, task_ids: list[str]) -> dict[str, Task]:
    """Load bundle task rows and their evidence relationships read-only."""
    rows = (
        session.execute(
            select(Task)
            .where(Task.id.in_(task_ids))
            .options(
                selectinload(Task.worker_runs),
                selectinload(Task.timeline_events),
                selectinload(Task.human_interactions),
            )
        )
        .scalars()
        .all()
    )
    result = {str(task.id): task for task in rows}
    if set(result) != set(task_ids):
        missing = sorted(set(task_ids) - set(result))
        raise ValueError(f"bundle task rows missing from database: {missing}")
    return result


def _manifest_case(case, outcome, task: Task, policy: ReliabilityReportPolicy) -> Wave3ManifestCase:
    """Reconcile one private outcome with persisted identity and exclusion state."""
    reason, _, _, _ = _validate_task_candidate(task, policy)
    runs = sorted(
        task.worker_runs,
        key=lambda run: (run.started_at or datetime.min.replace(tzinfo=UTC), str(run.id)),
    )
    identity, identity_status = resolve_task_execution_identity(task, runs)
    expected = policy.expected_execution_identities.get(case.worker_profile)
    identity_matches = identity_status == "verified" and identity is not None
    if identity_matches and expected is not None:
        identity_matches = (
            identity.provider == expected.provider
            and identity.model == expected.model
            and identity.reasoning_effort == expected.reasoning_effort
        )
    exclusion_reason = reason or check_cohort_membership(
        policy.evidence_scope,
        case.worker_profile,
        identity,
        identity_status,
        policy.expected_execution_identities,
    )
    provider = "codex" if case.worker_profile.startswith("codex-") else "antigravity"
    failure_kind = (
        outcome.failure_kind if outcome.failure_kind in VALID_FAILURE_KINDS else "unknown"
    )
    return Wave3ManifestCase(
        case_id=case.case_id,
        pair_group=case.pair_group or "",
        pair_order=case.pair_order or 0,
        task_class=case.task_class,
        worker_profile=case.worker_profile,
        provider=provider,
        terminal_status=outcome.terminal_status,
        accepted=outcome.terminal_status == "completed",
        failure_kind=failure_kind if outcome.terminal_status == "failed" else None,
        time_to_terminal_seconds=outcome.time_to_terminal_seconds,
        execution_identity_status=identity_status,
        identity_matches=identity_matches,
        exclusion_reason=exclusion_reason,
    )


def build_manifest(args: argparse.Namespace) -> Wave3Manifest:
    """Build and validate the sanitized manifest from bundle and Postgres state."""
    suite = M29EvidenceSuite.model_validate(json.loads(args.suite.read_text(encoding="utf-8")))
    bundle = M29EvidenceBundle.model_validate(
        json.loads((args.bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    )
    if set(bundle.cases) != {case.case_id for case in suite.cases}:
        raise ValueError("bundle cases do not exactly match the frozen suite")

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
            tasks = _load_tasks(session, task_ids)
            cases = [
                _manifest_case(
                    next(case for case in suite.cases if case.case_id == case_id),
                    outcome,
                    tasks[outcome.task_id],
                    policy,
                )
                for case_id, outcome in sorted(bundle.cases.items())
            ]
    finally:
        engine.dispose()

    manifest = Wave3Manifest(
        suite_name=suite.suite_name,
        suite_sha256=_sha256(args.suite),
        build_sha=bundle.identity.build_sha,
        target_repository_revision=bundle.identity.target_repository_revision,
        as_of=as_of,
        advisory_report_sha256=_sha256(args.advisory_report),
        operational_report_sha256=_sha256(args.operational_report),
        robustness_report_sha256=_sha256(args.robustness_report),
        cases=cases,
    )
    assert_sanitized_manifest(manifest.model_dump(mode="json"))
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
        LOGGER.error("Failed to build sanitized manifest: %s", exc)
        return 1
    print(f"Wrote sanitized Wave 3 manifest to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
