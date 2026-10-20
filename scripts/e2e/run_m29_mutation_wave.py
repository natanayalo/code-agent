#!/usr/bin/env python3
"""Operator CLI to initialize, preflight, execute, and sweep M29 mutation evaluation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evaluation.m29_mutation_dispatcher import (
    DisposableDbMemoryIsolationManager,
    HttpStackCandidateDispatcher,
)
from evaluation.m29_mutation_fixture import (
    PINNED_TASKBOARD_FIXTURE_REVISION,
    create_taskboard_fixture_repo,
    verify_taskboard_fixture_repo,
)
from evaluation.m29_mutation_isolation import MAX_EVALUATION_RETENTION_SECONDS
from evaluation.m29_mutation_oracles import compute_verifier_source_sha256
from evaluation.m29_mutation_publication import (
    extract_mutation_baseline_snapshot,
    reconcile_and_publish_mutation_wave,
)
from evaluation.m29_mutation_reviewer_client import OpenRouterNeutralReviewerClient
from evaluation.m29_mutation_runner import M29MutationRunner
from evaluation.m29_mutation_runner_models import M29MutationRunnerResult
from evaluation.m29_mutation_runner_preflight import run_mutation_preflight_checks
from evaluation.m29_mutation_suite import (
    M29_MUTATION_SUITE_NAME,
    M29MutationCase,
    build_m29_mutation_suite,
)
from evaluation.m29_mutation_verifier import ReadOnlyExternalEvaluator
from evaluation.m29_mutation_watchdog import EvaluationRetentionWatchdog
from evaluation.provider_reliability_report import render_json_report
from orchestrator.state import MemoryContext
from sandbox.workspace import WorkspaceManager

LOGGER = logging.getLogger("run_m29_mutation_wave")


def _json_serial(obj: Any) -> Any:
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _load_bundle(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / "bundle.json"
    if not path.exists():
        raise FileNotFoundError(f"bundle.json not found in {bundle_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_bundle(bundle_dir: Path, data: dict[str, Any]) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    path = bundle_dir / "bundle.json"
    temp = path.with_suffix(".tmp")
    payload = json.dumps(data, indent=2, sort_keys=True, default=_json_serial)
    temp.write_text(payload, encoding="utf-8")
    temp.replace(path)


def handle_init(args: argparse.Namespace) -> int:
    """Initialize the disposable fixture repository and evaluation bundle."""
    if not args.ack_disposable_mutation_stack:
        sys.stderr.write(
            "ERROR: --ack-disposable-mutation-stack is required to operate on disposable stacks.\n"
        )
        return 2

    bundle_dir = Path(args.bundle_dir).resolve()
    fixture_dir = Path(args.fixture_repo_path).resolve()

    print(f"Initializing taskboard fixture repository at {fixture_dir}...")
    create_taskboard_fixture_repo(fixture_dir)

    errs = verify_taskboard_fixture_repo(
        fixture_dir, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
    )
    if errs:
        sys.stderr.write(f"ERROR: Fixture repository verification failed: {'; '.join(errs)}\n")
        return 1

    verifier_sha = compute_verifier_source_sha256()
    bundle_dir.mkdir(parents=True, exist_ok=True)

    explicit_build_sha = getattr(args, "build_sha", None)
    if explicit_build_sha and explicit_build_sha.strip():
        build_sha = explicit_build_sha.strip()
    else:
        try:
            import subprocess

            build_sha = (
                subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
                .decode("utf-8")
                .strip()
            )
        except Exception:
            build_sha = "0000000"

    bundle_data = {
        "suite_name": M29_MUTATION_SUITE_NAME,
        "status": "initialized",
        "initialized_at": datetime.now(UTC).isoformat(),
        "fixture_repo_path": str(fixture_dir),
        "fixture_revision": PINNED_TASKBOARD_FIXTURE_REVISION,
        "verifier_source_sha256": verifier_sha,
        "build_sha": build_sha,
    }
    _save_bundle(bundle_dir, bundle_data)

    EvaluationRetentionWatchdog(bundle_dir)
    print(f"Successfully initialized M29 mutation wave bundle in {bundle_dir} (build={build_sha})")
    return 0


def handle_preflight(args: argparse.Namespace) -> int:
    """Execute preflight checks and snapshot verification against disposable database."""
    if not args.ack_disposable_mutation_stack:
        sys.stderr.write(
            "ERROR: --ack-disposable-mutation-stack is required to operate on disposable stacks.\n"
        )
        return 2

    bundle_dir = Path(args.bundle_dir).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    fixture_dir = Path(args.fixture_repo_path).resolve()
    bundle_data = _load_bundle(bundle_dir)

    suite = build_m29_mutation_suite()
    isolation_mgr = DisposableDbMemoryIsolationManager(args.db_url)
    baseline_mem = isolation_mgr.load_memory_context("taskboard")

    as_of_arg = getattr(args, "as_of", None)
    if as_of_arg:
        raw_as_of = as_of_arg[:-1] + "+00:00" if as_of_arg.endswith("Z") else as_of_arg
        effective_as_of = datetime.fromisoformat(raw_as_of)
        if effective_as_of.tzinfo is None:
            effective_as_of = effective_as_of.replace(tzinfo=UTC)
    else:
        effective_as_of = datetime.now(UTC) + timedelta(hours=24)

    baseline_ids, baseline_report = extract_mutation_baseline_snapshot(args.db_url, effective_as_of)
    if any(len(ids) > 0 for ids in baseline_ids.values()):
        baseline_path = bundle_dir / "baseline_report.json"
        baseline_path.write_text(render_json_report(baseline_report), encoding="utf-8")
        total_baseline = sum(len(ids) for ids in baseline_ids.values())
        print(f"Captured baseline report with {total_baseline} existing tasks")

    print(f"Running M29 mutation preflight checks against {args.db_url}...")
    try:
        snap = run_mutation_preflight_checks(
            db_url=args.db_url,
            workspace_root=workspace_root,
            retention_seconds=MAX_EVALUATION_RETENTION_SECONDS,
            suite=suite,
            baseline_memory_context=baseline_mem,
            fixture_repo_path=fixture_dir,
            expected_verifier_sha256=compute_verifier_source_sha256(),
            as_of=effective_as_of,
            baseline_ids_by_profile=baseline_ids,
        )
    except Exception as exc:
        sys.stderr.write(f"ERROR: Preflight check failed: {exc}\n")
        return 1

    bundle_data["status"] = "preflight_passed"
    bundle_data["preflight_passed_at"] = datetime.now(UTC).isoformat()
    bundle_data["task_cohort_snapshot"] = asdict(snap)
    _save_bundle(bundle_dir, bundle_data)

    print(f"Preflight checks passed! Baseline hash: {snap.baseline_memory_context_hash}")
    return 0


def _extract_cohort_as_of_and_baseline_ids(
    bundle_data: dict[str, Any],
) -> tuple[datetime | None, Mapping[str, Any] | None]:
    """Extract and parse frozen as_of and baseline task IDs from task cohort snapshot."""
    as_of: datetime | None = None
    baseline_ids: Mapping[str, Any] | None = None
    cohort_snap = bundle_data.get("task_cohort_snapshot")
    if isinstance(cohort_snap, dict):
        raw_as_of = cohort_snap.get("as_of")
        if isinstance(raw_as_of, str):
            if raw_as_of.endswith("Z"):
                raw_as_of = raw_as_of[:-1] + "+00:00"
            as_of = datetime.fromisoformat(raw_as_of)
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=UTC)
        elif isinstance(raw_as_of, datetime):
            as_of = raw_as_of
        baseline_ids = cohort_snap.get("baseline_ids_by_profile")
    return as_of, baseline_ids


def _save_run_results(
    bundle_dir: Path, bundle_data: dict[str, Any], res: M29MutationRunnerResult
) -> None:
    """Save raw entries, sanitized cases, and updated bundle manifest to disk."""
    raw_path = bundle_dir / "raw_entries.json"
    raw_payload = [asdict(r) for r in res.raw_entries]
    raw_path.write_text(json.dumps(raw_payload, indent=2, default=_json_serial), encoding="utf-8")

    san_path = bundle_dir / "sanitized_cases.json"
    san_payload = [s.model_dump(mode="json") for s in res.sanitized_cases]
    san_path.write_text(json.dumps(san_payload, indent=2, default=_json_serial), encoding="utf-8")

    bundle_data.update(
        {
            "status": res.status,
            "stop_reason": res.stop_reason,
            "case_count": len(res.raw_entries),
            "sanitized_case_count": len(res.sanitized_cases),
            "workspaces_cleaned_count": len(res.workspaces_cleaned),
            "workspaces_retained_count": len(res.workspaces_retained),
            "sanitized_archive_checksum": res.sanitized_archive_checksum,
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )
    _save_bundle(bundle_dir, bundle_data)


def handle_run(args: argparse.Namespace) -> int:
    """Execute full 20-case mutation evaluation against disposable stack."""
    if not args.ack_disposable_mutation_stack:
        sys.stderr.write(
            "ERROR: --ack-disposable-mutation-stack is required to operate on disposable stacks.\n"
        )
        return 2

    bundle_dir = Path(args.bundle_dir).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    fixture_dir = Path(args.fixture_repo_path).resolve()
    bundle_data = _load_bundle(bundle_dir)

    suite = build_m29_mutation_suite()
    watchdog = EvaluationRetentionWatchdog(bundle_dir, retention_seconds=args.retention_seconds)
    isolation_mgr = DisposableDbMemoryIsolationManager(args.db_url)
    baseline_mem = isolation_mgr.load_memory_context("taskboard")

    as_of, baseline_ids = _extract_cohort_as_of_and_baseline_ids(bundle_data)
    expected_verifier_sha = (
        bundle_data.get("verifier_source_sha256") or compute_verifier_source_sha256()
    )

    dispatcher = HttpStackCandidateDispatcher(
        api_base_url=args.api_base_url,
        db_url=args.db_url,
        workspace_root=workspace_root,
        repo_key="taskboard",
        branch="main",
        auth_token=args.auth_token,
        retention_watchdog=watchdog,
        memory_isolation_manager=isolation_mgr,
    )
    reviewer = OpenRouterNeutralReviewerClient(
        api_key=args.openrouter_api_key,
        base_url=args.openrouter_base_url,
    )
    evaluator = ReadOnlyExternalEvaluator(reviewer_client=reviewer)
    wm = WorkspaceManager(workspace_root)

    def _provide_case_memory(case: M29MutationCase) -> MemoryContext:
        isolation_mgr.discard_case_memory_writes()
        return isolation_mgr.load_memory_context("taskboard")

    runner = M29MutationRunner(
        suite=suite,
        db_url=args.db_url,
        workspace_manager=wm,
        baseline_memory_context=baseline_mem,
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        retention_seconds=args.retention_seconds,
        fixture_repo_path=fixture_dir,
        expected_verifier_sha256=expected_verifier_sha,
        as_of=as_of,
        baseline_ids_by_profile=baseline_ids,
        retention_watchdog=watchdog,
        memory_context_provider=_provide_case_memory,
    )

    print(f"Starting M29 feature/mutation wave ({len(suite.cases)} cases)...")
    res: M29MutationRunnerResult = runner.run()

    _save_run_results(bundle_dir, bundle_data, res)

    print(
        f"M29 wave run complete: status={res.status}, "
        f"cleaned={len(res.workspaces_cleaned)}, retained={len(res.workspaces_retained)}"
    )
    if res.stop_reason:
        print(f"Stop reason: {res.stop_reason}")
    return 0 if res.status == "completed" else 1


def handle_sweep_retention(args: argparse.Namespace) -> int:
    """Sweep expired workspaces recorded in the retention watchdog ledger."""
    bundle_dir = Path(args.bundle_dir).resolve()
    workspace_root = Path(args.workspace_root).resolve()

    watchdog = EvaluationRetentionWatchdog(bundle_dir)
    wm = WorkspaceManager(workspace_root)

    print(f"Sweeping retention watchdog ledger in {bundle_dir} (force_all={args.force_all})...")
    result = watchdog.sweep_expired(wm, force_all=args.force_all)

    print(
        f"Sweep finished: total_recorded={result.total_recorded}, "
        f"expired={result.expired_count}, cleaned={result.cleaned_count}, "
        f"failed={result.failed_count}"
    )
    if result.errors:
        for ws_id, err in result.errors.items():
            print(f"  [ERROR] {ws_id}: {err}")
    return 0 if result.failed_count == 0 else 1


def handle_publish(args: argparse.Namespace) -> int:
    """Execute post-cutoff cohort reconciliation and publish sanitized M29 reports."""
    if not args.ack_disposable_mutation_stack:
        sys.stderr.write(
            "ERROR: --ack-disposable-mutation-stack is required to operate on disposable stacks.\n"
        )
        return 2

    bundle_dir = Path(args.bundle_dir).resolve()
    bundle_data = _load_bundle(bundle_dir)
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (bundle_dir / "published")

    print(f"Reconciling and publishing M29 mutation wave to {output_dir}...")
    try:
        artifacts = reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir,
            db_url=args.db_url,
            output_dir=output_dir,
            build_sha=args.build_sha,
            bootstrap_iterations=args.bootstrap_iterations,
            bootstrap_seed=args.bootstrap_seed,
        )
    except Exception as exc:
        LOGGER.exception("Failed to reconcile and publish mutation wave: %s", exc)
        sys.stderr.write(f"ERROR: Publication failed: {exc}\n")
        return 1

    bundle_data["status"] = "published"
    bundle_data["published_at"] = datetime.now(UTC).isoformat()
    bundle_data["manifest_sha256"] = artifacts.manifest_sha256
    bundle_data["published_output_dir"] = str(output_dir)
    _save_bundle(bundle_dir, bundle_data)

    print(f"Successfully published M29 mutation wave to {output_dir}")
    print(f"Manifest SHA-256: {artifacts.manifest_sha256}")
    print(f"Written files ({len(artifacts.written_files)}):")
    for f in artifacts.written_files:
        print(f"  - {f.name}")
    return 0


def handle_status(args: argparse.Namespace) -> int:
    """Print status of bundle and retention watchdog ledger."""
    bundle_dir = Path(args.bundle_dir).resolve()
    bundle_data = _load_bundle(bundle_dir)

    print("=== M29 Mutation Wave Bundle Status ===")
    print(f"Suite: {bundle_data.get('suite_name')}")
    print(f"Status: {bundle_data.get('status')}")
    if bundle_data.get("stop_reason"):
        print(f"Stop Reason: {bundle_data.get('stop_reason')}")
    print(f"Initialized At: {bundle_data.get('initialized_at')}")
    if bundle_data.get("finished_at"):
        print(f"Finished At: {bundle_data.get('finished_at')}")

    watchdog = EvaluationRetentionWatchdog(bundle_dir)
    records = watchdog.records
    uncleaned = watchdog.uncleaned_records
    print("\n=== Retention Watchdog Ledger ===")
    print(f"Total Recorded Workspaces: {len(records)}")
    print(f"Currently Uncleaned: {len(uncleaned)}")
    for r in records:
        state = "CLEANED" if r.cleaned_up else "RETAINED"
        err = f" (error: {r.cleanup_error})" if r.cleanup_error else ""
        print(
            f"  [{state}] ws={r.workspace_id} task={r.task_id} "
            f"deadline={r.retention_deadline_at}{err}"
        )

    return 0


def _add_lifecycle_subparsers(subparsers: Any) -> None:
    """Add init, preflight, run, and publish lifecycle parsers."""
    # init
    init_p = subparsers.add_parser("init", help="Initialize fixture repo and bundle")
    init_p.add_argument("--bundle-dir", required=True, help="Target evaluation bundle directory")
    init_p.add_argument("--fixture-repo-path", required=True, help="Fixture repo destination path")
    init_p.add_argument("--build-sha", default=None, help="Explicit build SHA (defaults to HEAD)")
    init_p.add_argument(
        "--ack-disposable-mutation-stack",
        action="store_true",
        default=False,
        help="Acknowledge running on an isolated disposable stack",
    )

    # preflight
    pref_p = subparsers.add_parser(
        "preflight", help="Run preflight gates and record cohort snapshot"
    )
    pref_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")
    pref_p.add_argument("--db-url", required=True, help="Disposable database URL")
    pref_p.add_argument("--workspace-root", required=True, help="Evaluation workspace root path")
    pref_p.add_argument("--fixture-repo-path", required=True, help="Fixture repo path")
    pref_p.add_argument("--as-of", default=None, help="Frozen as-of cutoff (ISO-8601 UTC)")
    pref_p.add_argument(
        "--ack-disposable-mutation-stack",
        action="store_true",
        default=False,
        help="Acknowledge running on an isolated disposable stack",
    )

    # run
    run_p = subparsers.add_parser("run", help="Execute 20-case mutation evaluation")
    run_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")
    run_p.add_argument("--db-url", required=True, help="Disposable database URL")
    run_p.add_argument("--workspace-root", required=True, help="Evaluation workspace root path")
    run_p.add_argument("--fixture-repo-path", required=True, help="Fixture repo path")
    run_p.add_argument("--api-base-url", default="http://127.0.0.1:8000", help="API base URL")
    run_p.add_argument("--openrouter-api-key", default=os.environ.get("OPENROUTER_API_KEY", ""))
    run_p.add_argument("--openrouter-base-url", default="https://openrouter.ai/api/v1")
    run_p.add_argument("--retention-seconds", type=int, default=MAX_EVALUATION_RETENTION_SECONDS)
    run_p.add_argument("--auth-token", default=os.environ.get("API_SHARED_SECRET"))
    run_p.add_argument(
        "--ack-disposable-mutation-stack",
        action="store_true",
        default=False,
        help="Acknowledge running on an isolated disposable stack",
    )

    # publish
    pub_p = subparsers.add_parser("publish", help="Reconcile and publish M29 mutation reports")
    pub_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")
    pub_p.add_argument("--db-url", required=True, help="Disposable database URL")
    pub_p.add_argument(
        "--output-dir", default=None, help="Output directory for published artifacts"
    )
    pub_p.add_argument("--build-sha", default=None, help="Explicit build SHA")
    pub_p.add_argument(
        "--bootstrap-iterations", type=int, default=10000, help="Robustness bootstrap iterations"
    )
    pub_p.add_argument(
        "--bootstrap-seed", type=int, default=29, help="Deterministic bootstrap random seed"
    )
    pub_p.add_argument(
        "--ack-disposable-mutation-stack",
        action="store_true",
        default=False,
        help="Acknowledge running on an isolated disposable stack",
    )


def _add_maintenance_subparsers(subparsers: Any) -> None:
    """Add sweep-retention and status maintenance parsers."""
    # sweep-retention
    swp_p = subparsers.add_parser("sweep-retention", help="Sweep expired evaluation workspaces")
    swp_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")
    swp_p.add_argument("--workspace-root", required=True, help="Evaluation workspace root path")
    swp_p.add_argument("--force-all", action="store_true", default=False, help="Force cleanup all")

    # status
    stat_p = subparsers.add_parser("status", help="Print bundle and retention ledger status")
    stat_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for M29 mutation wave operations."""
    parser = argparse.ArgumentParser(
        description="Operator CLI for M29 feature/mutation disposable-stack evaluation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_lifecycle_subparsers(subparsers)
    _add_maintenance_subparsers(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "init": handle_init,
        "preflight": handle_preflight,
        "run": handle_run,
        "publish": handle_publish,
        "sweep-retention": handle_sweep_retention,
        "status": handle_status,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
