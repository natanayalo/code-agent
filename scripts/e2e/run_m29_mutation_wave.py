#!/usr/bin/env python3
"""Operator CLI to initialize, preflight, execute, and sweep M29 mutation evaluation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

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
from evaluation.m29_mutation_reviewer_client import (
    OpenRouterNeutralReviewerClient,
    build_custom_neutral_evaluator,
)
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
    return obj.__dict__ if hasattr(obj, "__dict__") else str(obj)


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

    exp_sha = getattr(args, "build_sha", None)
    build_sha = (exp_sha.strip() if exp_sha else "") or os.environ.get("BUILD_SHA", "")
    if not build_sha:
        try:
            build_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except Exception:
            build_sha = ""
    if not build_sha or not re.match(r"^[0-9a-f]{7,64}$", build_sha):
        sys.stderr.write(
            "ERROR: Could not resolve valid build SHA from args, BUILD_SHA, or git HEAD\n"
        )
        return 1

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


def check_api_readiness(api_base_url: str, *, client: httpx.Client | None = None) -> None:
    """Verify disposable API stack /health and /ready endpoints."""
    base = api_base_url.rstrip("/")
    cli = client or httpx.Client(timeout=10.0)
    try:
        for ep, exp in [("/health", "ok"), ("/ready", "ready")]:
            resp = cli.get(f"{base}{ep}")
            if resp.status_code != 200:
                raise RuntimeError(f"API {ep} failed: status {resp.status_code} ({resp.text})")
            try:
                data = resp.json()
            except Exception as exc:
                raise RuntimeError(f"API {ep} response is not valid JSON: {exc}") from exc
            if data.get("status") != exp:
                raise RuntimeError(f"API {ep} returned non-{exp} status: {data}")
    finally:
        if client is None:
            cli.close()


def _verify_pre_run_readiness(api_base_url: str | None) -> bool:
    """Run readiness check before dispatching runs, logging any failure."""
    if not api_base_url:
        return True
    print(f"Verifying API readiness at {api_base_url} before dispatch...")
    try:
        check_api_readiness(api_base_url)
        print("API readiness verified (/health and /ready).")
        return True
    except Exception as exc:
        sys.stderr.write(f"ERROR: API readiness check failed: {exc}\n")
        return False


def _prepare_suite_and_baseline_memory(
    args: argparse.Namespace,
) -> tuple[Any, DisposableDbMemoryIsolationManager, MemoryContext]:
    """Build suite and load isolated baseline memory after discarding uncommitted writes."""
    eval_model = getattr(args, "evaluator_model", None)
    eval_prov = getattr(args, "evaluator_provider", None)
    neutral_eval = (
        build_custom_neutral_evaluator(
            eval_model, eval_prov if isinstance(eval_prov, str) else None
        )
        if isinstance(eval_model, str) and eval_model.strip()
        else None
    )
    suite = build_m29_mutation_suite(neutral_evaluator=neutral_eval)
    isolation_mgr = DisposableDbMemoryIsolationManager(args.db_url)
    isolation_mgr.discard_case_memory_writes()
    return suite, isolation_mgr, isolation_mgr.load_memory_context("taskboard")


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

    suite, isolation_mgr, baseline_mem = _prepare_suite_and_baseline_memory(args)

    print(f"Running M29 mutation preflight checks against {args.db_url}...")
    try:
        if getattr(args, "api_base_url", None):
            print(f"Checking API readiness at {args.api_base_url}...")
            check_api_readiness(args.api_base_url)
            print("API readiness verified (/health and /ready).")

        as_of_arg = getattr(args, "as_of", None)
        horizon_min = getattr(args, "as_of_horizon_minutes", None)
        if as_of_arg:
            raw_as_of = as_of_arg[:-1] + "+00:00" if as_of_arg.endswith("Z") else as_of_arg
            effective_as_of = datetime.fromisoformat(raw_as_of)
            if effective_as_of.tzinfo is None:
                effective_as_of = effective_as_of.replace(tzinfo=UTC)
        else:
            horizon = horizon_min if horizon_min is not None else 75
            effective_as_of = datetime.now(UTC) + timedelta(minutes=horizon)

        baseline_ids, baseline_report = extract_mutation_baseline_snapshot(
            args.db_url, effective_as_of
        )

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

    baseline_path = bundle_dir / "baseline_report.json"
    if any(len(ids) > 0 for ids in baseline_ids.values()):
        baseline_path.write_text(render_json_report(baseline_report), encoding="utf-8")
        total_baseline = sum(len(ids) for ids in baseline_ids.values())
        print(f"Captured baseline report with {total_baseline} existing tasks")
    elif baseline_path.exists():
        baseline_path.unlink()

    bundle_data["status"] = "preflight_passed"
    bundle_data["preflight_passed_at"] = datetime.now(UTC).isoformat()
    bundle_data["task_cohort_snapshot"] = asdict(snap)
    bundle_data["verifier_source_sha256"] = snap.verifier_source_sha256
    bundle_data["neutral_evaluator"] = suite.neutral_evaluator.model_dump(mode="json")
    _save_bundle(bundle_dir, bundle_data)

    print(f"Preflight checks passed! Baseline hash: {snap.baseline_memory_context_hash}")
    return 0


def _extract_cohort_as_of_and_baseline_ids(
    bundle_data: dict[str, Any],
) -> tuple[datetime | None, Mapping[str, Any] | None]:
    """Extract and parse frozen as_of and baseline task IDs from task cohort snapshot."""
    snap = bundle_data.get("task_cohort_snapshot") or {}
    raw_as_of = snap.get("as_of")
    baselines = snap.get("baseline_ids_by_profile")
    if isinstance(raw_as_of, str):
        raw = raw_as_of[:-1] + "+00:00" if raw_as_of.endswith("Z") else raw_as_of
        as_of = datetime.fromisoformat(raw)
        return (as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of), baselines
    return (raw_as_of if isinstance(raw_as_of, datetime) else None), baselines


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
    if res.snapshot is not None:
        bundle_data.setdefault("task_cohort_snapshot", asdict(res.snapshot))
    _save_bundle(bundle_dir, bundle_data)


def _build_mutation_runner(
    args: argparse.Namespace,
    bundle_data: dict[str, Any],
    workspace_root: Path,
    fixture_dir: Path,
    watchdog: EvaluationRetentionWatchdog,
    isolation_mgr: DisposableDbMemoryIsolationManager,
    baseline_mem: MemoryContext,
    suite: Any,
) -> M29MutationRunner:
    """Build runner with configured candidate dispatcher, evaluator, and memory isolation."""
    as_of, baseline_ids = _extract_cohort_as_of_and_baseline_ids(bundle_data)
    expected_verifier_sha = (
        bundle_data.get("verifier_source_sha256") or compute_verifier_source_sha256()
    )
    dispatcher = HttpStackCandidateDispatcher(
        api_base_url=args.api_base_url,
        db_url=args.db_url,
        workspace_root=workspace_root,
        repo_key="taskboard",
        branch=getattr(args, "branch", "master"),
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

    return M29MutationRunner(
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

    if not _verify_pre_run_readiness(getattr(args, "api_base_url", None)):
        return 1

    suite, isolation_mgr, baseline_mem = _prepare_suite_and_baseline_memory(args)
    watchdog = EvaluationRetentionWatchdog(bundle_dir, retention_seconds=args.retention_seconds)

    runner = _build_mutation_runner(
        args, bundle_data, workspace_root, fixture_dir, watchdog, isolation_mgr, baseline_mem, suite
    )

    print(f"Starting M29 feature/mutation wave ({len(suite.cases)} cases)...")
    res: M29MutationRunnerResult = runner.run()

    bundle_data["neutral_evaluator"] = suite.neutral_evaluator.model_dump(mode="json")
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
    ws_root = Path(args.workspace_root).resolve()
    print(f"Sweeping retention watchdog ledger in {bundle_dir} (force_all={args.force_all})...")
    result = EvaluationRetentionWatchdog(bundle_dir).sweep_expired(
        WorkspaceManager(ws_root), force_all=args.force_all
    )
    print(
        f"Sweep finished: total_recorded={result.total_recorded}, "
        f"expired={result.expired_count}, cleaned={result.cleaned_count}, "
        f"failed={result.failed_count}"
    )
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
    for k in ("suite_name", "status", "stop_reason", "initialized_at", "finished_at"):
        if bundle_data.get(k):
            print(f"{k.replace('_', ' ').title()}: {bundle_data.get(k)}")

    watchdog = EvaluationRetentionWatchdog(bundle_dir)
    print("\n=== Retention Watchdog Ledger ===")
    print(f"Total Recorded Workspaces: {len(watchdog.records)}")
    print(f"Currently Uncleaned: {len(watchdog.uncleaned_records)}")
    for r in watchdog.records:
        state = "CLEANED" if r.cleaned_up else "RETAINED"
        err = f" (error: {r.cleanup_error})" if r.cleanup_error else ""
        print(
            f"  [{state}] ws={r.workspace_id} task={r.task_id} "
            f"deadline={r.retention_deadline_at}{err}"
        )

    return 0


def _add_ack_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ack-disposable-mutation-stack",
        action="store_true",
        default=False,
        help="Acknowledge running on an isolated disposable stack",
    )


def _add_lifecycle_subparsers(subparsers: Any) -> None:
    """Add init, preflight, run, and publish lifecycle parsers."""
    # init
    init_p = subparsers.add_parser("init", help="Initialize fixture repo and bundle")
    init_p.add_argument("--bundle-dir", required=True, help="Target evaluation bundle directory")
    init_p.add_argument("--fixture-repo-path", required=True, help="Fixture repo destination path")
    init_p.add_argument("--build-sha", default=None, help="Explicit build SHA (defaults to HEAD)")
    _add_ack_arg(init_p)

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
        "--as-of-horizon-minutes",
        type=int,
        default=75,
        help="Minutes from now for as_of cutoff when --as-of is omitted (default: 75)",
    )
    pref_p.add_argument(
        "--api-base-url",
        default=None,
        help="Optional API base URL to verify /health and /ready during preflight",
    )
    pref_p.add_argument("--evaluator-model", default=None, help="Custom evaluator model slug")
    pref_p.add_argument("--evaluator-provider", default=None, help="Custom evaluator provider")
    _add_ack_arg(pref_p)

    # run
    run_p = subparsers.add_parser("run", help="Execute 20-case mutation evaluation")
    run_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")
    run_p.add_argument("--db-url", required=True, help="Disposable database URL")
    run_p.add_argument("--workspace-root", required=True, help="Evaluation workspace root path")
    run_p.add_argument("--fixture-repo-path", required=True, help="Fixture repo path")
    run_p.add_argument(
        "--branch", default="master", help="Target git branch to clone (default: master)"
    )
    run_p.add_argument("--api-base-url", default="http://127.0.0.1:8000", help="API base URL")
    run_p.add_argument("--openrouter-api-key", default=os.environ.get("OPENROUTER_API_KEY", ""))
    run_p.add_argument("--openrouter-base-url", default="https://openrouter.ai/api/v1")
    run_p.add_argument("--retention-seconds", type=int, default=MAX_EVALUATION_RETENTION_SECONDS)
    run_p.add_argument("--evaluator-model", default=None, help="Custom evaluator model slug")
    run_p.add_argument("--evaluator-provider", default=None, help="Custom evaluator provider")
    token_default = os.getenv("CODE_AGENT_API_SHARED_SECRET") or os.getenv("API_SHARED_SECRET")
    run_p.add_argument("--auth-token", default=token_default)
    _add_ack_arg(run_p)

    # publish
    pub_p = subparsers.add_parser("publish", help="Reconcile and publish M29 mutation reports")
    pub_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")
    pub_p.add_argument("--db-url", required=True, help="Disposable database URL")
    pub_p.add_argument("--output-dir", default=None, help="Published output dir")
    pub_p.add_argument("--build-sha", default=None, help="Explicit build SHA")
    pub_p.add_argument("--bootstrap-iterations", type=int, default=10000)
    pub_p.add_argument("--bootstrap-seed", type=int, default=29)
    _add_ack_arg(pub_p)


def _add_maintenance_subparsers(subparsers: Any) -> None:
    """Add sweep-retention and status maintenance parsers."""
    swp_p = subparsers.add_parser("sweep-retention", help="Sweep expired evaluation workspaces")
    swp_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")
    swp_p.add_argument("--workspace-root", required=True, help="Evaluation workspace root path")
    swp_p.add_argument("--force-all", action="store_true", default=False, help="Force cleanup all")
    stat_p = subparsers.add_parser("status", help="Print bundle and retention ledger status")
    stat_p.add_argument("--bundle-dir", required=True, help="Evaluation bundle directory")


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for M29 mutation wave operations."""
    parser = argparse.ArgumentParser(description="Operator CLI for M29 mutation evaluation.")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_lifecycle_subparsers(sub)
    _add_maintenance_subparsers(sub)
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
    return handler(args) if handler else (parser.print_help() or 1)


if __name__ == "__main__":
    sys.exit(main())
