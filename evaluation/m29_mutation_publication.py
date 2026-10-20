"""Post-cutoff cohort reconciliation and sanitized report publication for M29 mutation."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evaluation.m29_mutation_cohort import (
    M29_MUTATION_PROFILES,
    M29MutationCohortReconciliation,
    reconcile_m29_mutation_cohort,
)
from evaluation.m29_mutation_manifest import (
    M29MutationManifest,
    M29MutationManifestCase,
    M29MutationPairedAnalysisReport,
    assert_sanitized_mutation_paired_report,
    build_mutation_paired_report,
    manifest_sha256,
    render_mutation_paired_markdown,
)
from evaluation.m29_mutation_runner_archive import (
    reconcile_m29_publication_gate,
    validate_sanitized_archive,
)
from evaluation.m29_mutation_suite import (
    M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES,
    M29MutationSuite,
    build_m29_mutation_suite,
)
from evaluation.provider_reliability_extractor import (
    ExtractedTaskEvidence,
    build_provider_reliability_report,
    load_task_evidence_snapshot,
)
from evaluation.provider_reliability_models import (
    ProviderReliabilityReport,
    ProviderReliabilityRobustnessPolicy,
    ProviderReliabilityRobustnessReport,
    ReliabilityReportPolicy,
)
from evaluation.provider_reliability_report import (
    render_json_report,
    render_markdown_report,
)
from evaluation.provider_reliability_robustness import (
    evaluate_robustness_snapshot,
)
from evaluation.provider_reliability_robustness_report import (
    render_json_robustness_report,
    render_markdown_robustness_report,
)

LOGGER = logging.getLogger("m29_mutation_publication")


@dataclass(frozen=True)
class M29MutationPublicationArtifacts:
    """Sanitized public manifest, paired supplement, reports, and written paths."""

    manifest: M29MutationManifest
    paired_report: M29MutationPairedAnalysisReport
    canonical_report: ProviderReliabilityReport
    operational_report: ProviderReliabilityReport
    robustness_report: ProviderReliabilityRobustnessReport
    manifest_sha256: str
    written_files: tuple[Path, ...]


def _parse_utc_datetime(val: str | datetime) -> datetime:
    """Parse string or datetime into UTC timezone-aware datetime."""
    if isinstance(val, str):
        raw = val[:-1] + "+00:00" if val.endswith("Z") else val
        dt = datetime.fromisoformat(raw)
    else:
        dt = val
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _write_atomic(path: Path, content: str) -> None:
    """Write text atomically to a path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f"{path.suffix}.pending")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def _resolve_build_sha(explicit_sha: str | None, bundle_sha: str | None) -> str:
    """Resolve and validate a 7-64 character lowercase hex build SHA."""
    if explicit_sha and explicit_sha.strip():
        sha = explicit_sha.strip()
    elif bundle_sha and bundle_sha.strip():
        sha = bundle_sha.strip()
    else:
        try:
            sha = (
                subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
                .decode("utf-8")
                .strip()
            )
        except Exception:
            sha = "0" * 40
    if not re.match(r"^[0-9a-f]{7,64}$", sha):
        raise ValueError(f"invalid build_sha '{sha}': must match ^[0-9a-f]{{7,64}}$")
    return sha


def extract_mutation_baseline_snapshot(
    db_url: str,
    as_of: datetime,
) -> tuple[dict[str, tuple[str, ...]], ProviderReliabilityReport]:
    """Capture pre-existing eligible feature/mutation task IDs and canonical baseline report."""
    as_of_utc = _parse_utc_datetime(as_of)
    window_start = as_of_utc - timedelta(days=90)
    policy = ReliabilityReportPolicy(
        schema_version=2,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=as_of_utc,
        window_start_at=window_start,
        window_end_at=as_of_utc,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES),
    )
    tasks, exclusions = load_task_evidence_snapshot(db_url, policy)
    report = build_provider_reliability_report(tasks, exclusions, policy)

    baseline_ids: dict[str, tuple[str, ...]] = {}
    for profile in M29_MUTATION_PROFILES:
        matching = [
            t.task_id
            for t in tasks
            if t.task_class == "feature"
            and t.mutation_mode == "mutation"
            and t.profile == profile
            and t.task_id is not None
        ]
        baseline_ids[profile] = tuple(sorted(set(matching)))

    return baseline_ids, report


def _load_and_validate_bundle_for_publication(
    bundle_dir: Path,
    effective_now: datetime,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[M29MutationManifestCase],
    datetime,
    datetime,
    datetime,
]:
    """Load bundle, raw entries, and sanitized cases, validating checksums and cutoff."""
    bundle_path = bundle_dir / "bundle.json"
    if not bundle_path.exists():
        raise FileNotFoundError(f"bundle.json not found in {bundle_dir}")
    bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))

    status = bundle_data.get("status")
    if status not in ("completed", "published"):
        raise ValueError(f"bundle status must be 'completed' or 'published', got '{status}'")

    raw_path = bundle_dir / "raw_entries.json"
    san_path = bundle_dir / "sanitized_cases.json"
    if not raw_path.exists():
        raise FileNotFoundError(f"raw_entries.json not found in {bundle_dir}")
    if not san_path.exists():
        raise FileNotFoundError(f"sanitized_cases.json not found in {bundle_dir}")

    raw_entries: list[dict[str, Any]] = json.loads(raw_path.read_text(encoding="utf-8"))
    san_raw: list[dict[str, Any]] = json.loads(san_path.read_text(encoding="utf-8"))
    if len(raw_entries) != 20:
        raise ValueError(f"raw_entries count mismatch: expected 20, got {len(raw_entries)}")
    if len(san_raw) != 20:
        raise ValueError(f"sanitized_cases count mismatch: expected 20, got {len(san_raw)}")

    sanitized_cases = [M29MutationManifestCase.model_validate(c) for c in san_raw]
    computed_checksum = validate_sanitized_archive(sanitized_cases)
    expected_checksum = bundle_data.get("sanitized_archive_checksum")
    if computed_checksum != expected_checksum:
        raise ValueError(
            f"sanitized archive checksum mismatch: expected '{expected_checksum}', "
            f"got '{computed_checksum}'"
        )

    cohort_snap = bundle_data.get("task_cohort_snapshot") or {}
    as_of = _parse_utc_datetime(cohort_snap["as_of"])
    window_start = _parse_utc_datetime(cohort_snap["window_start_at"])
    window_end = _parse_utc_datetime(cohort_snap["window_end_at"])

    if effective_now < as_of:
        raise ValueError(
            f"publication blocked: current time ({effective_now.isoformat()}) "
            f"is before cutoff ({as_of.isoformat()})"
        )

    return bundle_data, raw_entries, sanitized_cases, as_of, window_start, window_end


def _extract_post_cutoff_reports(
    db_url: str,
    as_of: datetime,
    window_start_at: datetime,
    window_end_at: datetime,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> tuple[
    list[ExtractedTaskEvidence],
    ProviderReliabilityReport,
    ProviderReliabilityReport,
    ProviderReliabilityRobustnessReport,
]:
    """Extract canonical, operational, and robustness reports from database snapshot."""
    canonical_policy = ReliabilityReportPolicy(
        schema_version=2,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=as_of,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES),
    )
    can_tasks, can_exclusions = load_task_evidence_snapshot(db_url, canonical_policy)
    canonical_report = build_provider_reliability_report(
        can_tasks, can_exclusions, canonical_policy
    )

    operational_policy = ReliabilityReportPolicy(
        schema_version=2,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=as_of,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
        evidence_scope="operational",
    )
    op_tasks, op_exclusions = load_task_evidence_snapshot(db_url, operational_policy)
    operational_report = build_provider_reliability_report(
        op_tasks, op_exclusions, operational_policy
    )

    robustness_policy = ProviderReliabilityRobustnessPolicy(
        schema_version=2,
        lookback_days=90,
        confidence_level=0.95,
        as_of=as_of,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
        evidence_scope="current_execution_cohort",
        expected_execution_identities=dict(M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES),
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed,
    )
    rob_tasks, rob_exclusions = load_task_evidence_snapshot(db_url, robustness_policy)
    robustness_report = evaluate_robustness_snapshot(rob_tasks, rob_exclusions, robustness_policy)

    return can_tasks, canonical_report, operational_report, robustness_report


def _cross_check_scheduled_tasks_with_db(
    raw_entries: list[dict[str, Any]],
    sanitized_cases: list[M29MutationManifestCase],
    canonical_tasks: list[ExtractedTaskEvidence],
    as_of: datetime,
) -> dict[str, datetime]:
    """Verify task outcomes against canonical DB snapshot and return terminal timestamps."""
    case_to_raw = {r["case_id"]: r for r in raw_entries}
    canonical_by_id = {t.task_id: t for t in canonical_tasks if t.task_id is not None}
    terminal_at_by_task_id: dict[str, datetime] = {}

    for c in sanitized_cases:
        r = case_to_raw[c.case_id]
        task_id = r["task_id"]
        t_snap = r.get("terminal_snapshot") or {}
        raw_ts = _parse_utc_datetime(t_snap.get("terminal_timestamp"))
        terminal_at_by_task_id[task_id] = raw_ts

        if raw_ts > as_of:
            raise ValueError(
                f"case '{c.case_id}' terminal timestamp ({raw_ts.isoformat()}) "
                f"is after frozen cutoff ({as_of.isoformat()})"
            )

        if c.exclusion_reason is None:
            if task_id not in canonical_by_id:
                raise ValueError(
                    f"eligible suite task '{task_id}' (case '{c.case_id}') "
                    "not found in canonical database snapshot"
                )
            db_ev = canonical_by_id[task_id]
            if db_ev.profile != c.worker_profile:
                raise ValueError(
                    f"task '{task_id}' profile mismatch: DB='{db_ev.profile}', "
                    f"manifest='{c.worker_profile}'"
                )
            if db_ev.accepted != c.accepted:
                raise ValueError(
                    f"task '{task_id}' accepted mismatch: DB={db_ev.accepted}, "
                    f"manifest={c.accepted}"
                )
            if abs((db_ev.terminal_timestamp - raw_ts).total_seconds()) > 1.0:
                raise ValueError(
                    f"task '{task_id}' terminal timestamp drift: "
                    f"DB={db_ev.terminal_timestamp.isoformat()}, archived={raw_ts.isoformat()}"
                )
        else:
            if task_id in canonical_by_id:
                raise ValueError(
                    f"excluded suite task '{task_id}' (case '{c.case_id}') "
                    "unexpectedly included in canonical database snapshot"
                )

    return terminal_at_by_task_id


def _cross_check_and_reconcile_cohort(
    raw_entries: list[dict[str, Any]],
    sanitized_cases: list[M29MutationManifestCase],
    canonical_tasks: list[ExtractedTaskEvidence],
    cohort_snap: dict[str, Any],
    as_of: datetime,
    window_start_at: datetime,
    window_end_at: datetime,
    effective_now: datetime,
) -> M29MutationCohortReconciliation:
    """Verify task outcomes against canonical DB snapshot and reconcile cohort."""
    terminal_at_by_task_id = _cross_check_scheduled_tasks_with_db(
        raw_entries, sanitized_cases, canonical_tasks, as_of
    )

    scheduled = {
        p: [r["task_id"] for r in raw_entries if r["worker_profile"] == p]
        for p in M29_MUTATION_PROFILES
    }
    case_to_task = {r["case_id"]: r["task_id"] for r in raw_entries}
    eligible = {
        p: [
            case_to_task[c.case_id]
            for c in sanitized_cases
            if c.worker_profile == p and c.exclusion_reason is None
        ]
        for p in M29_MUTATION_PROFILES
    }
    excluded = {
        p: [
            case_to_task[c.case_id]
            for c in sanitized_cases
            if c.worker_profile == p and c.exclusion_reason is not None
        ]
        for p in M29_MUTATION_PROFILES
    }
    current = {
        p: [
            t.task_id
            for t in canonical_tasks
            if t.task_class == "feature"
            and t.mutation_mode == "mutation"
            and t.profile == p
            and t.task_id is not None
        ]
        for p in M29_MUTATION_PROFILES
    }

    return reconcile_m29_mutation_cohort(
        as_of=as_of,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
        now=effective_now,
        baseline_ids_by_profile=cohort_snap.get("baseline_ids_by_profile", {}),
        frozen_baseline_ids_sha256_by_profile=cohort_snap.get(
            "frozen_baseline_ids_sha256_by_profile", {}
        ),
        scheduled_suite_ids_by_profile=scheduled,
        eligible_suite_ids_by_profile=eligible,
        excluded_suite_ids_by_profile=excluded,
        terminal_at_by_task_id=terminal_at_by_task_id,
        current_ids_by_profile=current,
        report_ids_by_profile=current,
    )


def _render_and_validate_publications(
    suite: M29MutationSuite,
    sanitized_cases: list[M29MutationManifestCase],
    cohort_recon: M29MutationCohortReconciliation,
    canonical_report: ProviderReliabilityReport,
    operational_report: ProviderReliabilityReport,
    robustness_report: ProviderReliabilityRobustnessReport,
    bundle_dir: Path,
    build_sha: str,
    as_of: datetime,
) -> tuple[M29MutationManifest, M29MutationPairedAnalysisReport, dict[str, str], str]:
    """Render JSON/MD artifacts, assert publication gates, and assemble output files."""
    can_json = render_json_report(canonical_report)
    can_md = render_markdown_report(canonical_report)
    op_json = render_json_report(operational_report)
    op_md = render_markdown_report(operational_report)
    rob_json = render_json_robustness_report(robustness_report)
    rob_md = render_markdown_robustness_report(robustness_report)

    advisory_sha = hashlib.sha256(can_json.encode("utf-8")).hexdigest()
    op_sha = hashlib.sha256(op_json.encode("utf-8")).hexdigest()
    rob_sha = hashlib.sha256(rob_json.encode("utf-8")).hexdigest()

    manifest = M29MutationManifest(
        schema_version=1,
        suite_name=suite.suite_name,
        suite_sha256=suite.compute_digest(),
        build_sha=build_sha,
        target_repository_revision=suite.target_repository_revision,
        as_of=as_of,
        advisory_report_sha256=advisory_sha,
        operational_report_sha256=op_sha,
        robustness_report_sha256=rob_sha,
        cohort_reconciliation=cohort_recon,
        cases=sanitized_cases,
    )

    baseline_report: ProviderReliabilityReport | None = None
    baseline_path = bundle_dir / "baseline_report.json"
    if baseline_path.exists():
        baseline_report = ProviderReliabilityReport.model_validate_json(
            baseline_path.read_text(encoding="utf-8")
        )

    reconcile_m29_publication_gate(
        manifest=manifest,
        suite=suite,
        canonical_report=canonical_report,
        robustness_report=robustness_report,
        baseline_report=baseline_report,
        canonical_report_json=can_json,
        robustness_report_json=rob_json,
        operational_report_json=op_json,
    )

    m_sha = manifest_sha256(manifest)
    paired_report = build_mutation_paired_report(manifest, manifest_sha256_hash=m_sha)
    assert_sanitized_mutation_paired_report(paired_report.model_dump(mode="json"))
    paired_md = render_mutation_paired_markdown(paired_report)

    file_contents = {
        "m29_provider_reliability_mutation_manifest.json": json.dumps(
            manifest.model_dump(mode="json"), indent=2, sort_keys=True
        ),
        "m29_provider_reliability_mutation_paired_analysis.json": json.dumps(
            paired_report.model_dump(mode="json"), indent=2, sort_keys=True
        ),
        "m29_provider_reliability_mutation_paired_analysis.md": paired_md + "\n",
        "m29_provider_reliability_report.json": can_json,
        "m29_provider_reliability_report.md": can_md,
        "m29_provider_reliability_operational_report.json": op_json,
        "m29_provider_reliability_operational_report.md": op_md,
        "m29_provider_reliability_robustness_report.json": rob_json,
        "m29_provider_reliability_robustness_report.md": rob_md,
    }

    return manifest, paired_report, file_contents, m_sha


def reconcile_and_publish_mutation_wave(
    *,
    bundle_dir: Path,
    db_url: str,
    output_dir: Path,
    build_sha: str | None = None,
    bootstrap_iterations: int = 10000,
    bootstrap_seed: int = 29,
    now: datetime | None = None,
) -> M29MutationPublicationArtifacts:
    """Execute post-cutoff cohort reconciliation and publish sanitized M29 reports."""
    effective_now = _parse_utc_datetime(now or datetime.now(UTC))
    suite = build_m29_mutation_suite()

    (
        bundle_data,
        raw_entries,
        sanitized_cases,
        as_of,
        window_start,
        window_end,
    ) = _load_and_validate_bundle_for_publication(bundle_dir, effective_now)

    effective_build_sha = _resolve_build_sha(build_sha, bundle_data.get("build_sha"))
    cohort_snap = bundle_data.get("task_cohort_snapshot") or {}

    (
        can_tasks,
        can_report,
        op_report,
        rob_report,
    ) = _extract_post_cutoff_reports(
        db_url, as_of, window_start, window_end, bootstrap_iterations, bootstrap_seed
    )

    cohort_recon = _cross_check_and_reconcile_cohort(
        raw_entries,
        sanitized_cases,
        can_tasks,
        cohort_snap,
        as_of,
        window_start,
        window_end,
        effective_now,
    )

    manifest, paired_rep, file_contents, m_sha = _render_and_validate_publications(
        suite=suite,
        sanitized_cases=sanitized_cases,
        cohort_recon=cohort_recon,
        canonical_report=can_report,
        operational_report=op_report,
        robustness_report=rob_report,
        bundle_dir=bundle_dir,
        build_sha=effective_build_sha,
        as_of=as_of,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, content in sorted(file_contents.items()):
        target = output_dir / filename
        _write_atomic(target, content)
        written.append(target)

    return M29MutationPublicationArtifacts(
        manifest=manifest,
        paired_report=paired_rep,
        canonical_report=can_report,
        operational_report=op_report,
        robustness_report=rob_report,
        manifest_sha256=m_sha,
        written_files=tuple(written),
    )


__all__ = [
    "M29MutationPublicationArtifacts",
    "extract_mutation_baseline_snapshot",
    "reconcile_and_publish_mutation_wave",
]
