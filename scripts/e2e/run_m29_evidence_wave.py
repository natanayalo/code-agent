#!/usr/bin/env python3
"""Operator CLI to execute the M29 28-task live provider evidence wave."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

try:
    import dotenv

    dotenv.load_dotenv()
except ImportError:
    pass

from evaluation.m29_evidence_models import (
    TOTAL_CASES,
    M29BundleIdentity,
    M29CaseOutcome,
    M29EvidenceBundle,
    M29EvidenceCase,
    M29EvidenceSuite,
)

LOGGER = logging.getLogger("run_m29_evidence_wave")
TERMINAL_STATUSES = {"completed", "failed"}


def _calculate_sha256(path: Path) -> str:
    """Calculate SHA-256 hex digest of file contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_suite(path: Path) -> M29EvidenceSuite:
    """Load and strictly validate the frozen suite JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return M29EvidenceSuite.model_validate(raw)


def _bundle_path(bundle_dir: Path) -> Path:
    return bundle_dir / "bundle.json"


def _load_bundle(bundle_dir: Path) -> M29EvidenceBundle:
    """Load existing evidence bundle from directory."""
    path = _bundle_path(bundle_dir)
    if not path.exists():
        raise FileNotFoundError(f"bundle not found at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return M29EvidenceBundle.model_validate(raw)


def _save_bundle(bundle_dir: Path, bundle: M29EvidenceBundle) -> None:
    """Atomically persist evidence bundle to disk."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    target = _bundle_path(bundle_dir)
    temp_target = target.with_suffix(".tmp")
    data = bundle.model_dump(mode="json")
    temp_target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp_target.replace(target)


def _client(base_url: str, api_token_env: str) -> httpx.Client:
    token = os.getenv(api_token_env)
    if not token:
        raise ValueError(f"required API token environment variable is unset: {api_token_env}")
    return httpx.Client(base_url=base_url.rstrip("/"), headers={"X-Webhook-Token": token})


def _check_stack_health(client: httpx.Client) -> None:
    """Verify API health and execution readiness before dispatching tasks."""
    try:
        health = client.get("/health", timeout=5.0)
        health.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"preflight /health check failed: {exc}") from exc

    try:
        ready = client.get("/ready", timeout=5.0)
        ready.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"preflight /ready check failed: {exc}") from exc


def _resolve_task_failure_kind(task_data: dict[str, Any]) -> str | None:
    """Resolve typed failure cause from execution-plan attempts and timeline."""
    if task_data.get("status") != "failed":
        return None
    plan = task_data.get("execution_plan") or {}
    for node in plan.get("nodes") or []:
        if node.get("failure_kind"):
            return str(node["failure_kind"])
        for attempt in node.get("attempts") or []:
            if attempt.get("failure_kind"):
                return str(attempt["failure_kind"])
    for event in reversed(task_data.get("timeline") or []):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("failure_kind"):
            return str(payload["failure_kind"])
        if event.get("event_type") in ("task_failed", "worker_run_failed") and payload.get(
            "reason"
        ):
            return str(payload["reason"])
    if task_data.get("last_error"):
        return "task_error"
    return "unknown"


def _get_changed_files_count(task_data: dict[str, Any]) -> int:
    """Determine number of files changed during task execution."""
    latest_run = task_data.get("latest_run") or {}
    files_changed = latest_run.get("files_changed") or []
    if files_changed:
        return len(files_changed)
    plan = task_data.get("execution_plan") or {}
    count = 0
    for node in plan.get("nodes") or []:
        count += len(node.get("changed_files") or [])
    return count


def _submit_case(
    client: httpx.Client,
    case: M29EvidenceCase,
    bundle_id: str,
    args: argparse.Namespace,
) -> str:
    """Submit single evidence task through webhook intake and return task_id."""
    delivery_id = hashlib.sha256(f"{bundle_id}:{case.case_id}".encode()).hexdigest()
    bundle_digest = hashlib.sha256(bundle_id.encode()).hexdigest()[:12]
    external_thread_id = f"m29-evidence-{case.case_id}-{bundle_digest}"
    worker_override = "codex" if "codex" in case.worker_profile else "antigravity"

    payload = {
        "task_text": case.prompt,
        "repo_key": args.repo_key,
        "branch": args.branch,
        "source": "m29-live-provider-evidence",
        "external_user_id": "m29-operator",
        "external_thread_id": external_thread_id,
        "delivery_id": delivery_id,
        "worker_override": worker_override,
        "worker_profile_override": case.worker_profile,
        "constraints": {
            "read_only": True,
            "delivery_mode": "summary",
        },
        "budget": {"worker_timeout_seconds": 600},
    }
    resp = client.post("/webhook", json=payload, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    task_id = str(data.get("task_id") or "")
    if not task_id:
        raise RuntimeError(f"webhook response missing task_id for case {case.case_id}")
    return task_id


def _poll_task(client: httpx.Client, task_id: str, timeout_seconds: int) -> dict[str, Any]:
    """Poll task until terminal status, failing closed on pause or cancellation."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        resp = client.get(f"/tasks/{task_id}", timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        current_status = data.get("status")
        if current_status == "cancelled":
            raise RuntimeError(f"task was cancelled: {task_id}")
        pending = data.get("pending_interactions") or []
        if pending:
            raise RuntimeError(f"task requires operator interaction: {task_id}")
        if current_status in TERMINAL_STATUSES:
            return data
        time.sleep(3)
    raise TimeoutError(f"task {task_id} did not reach terminal state within {timeout_seconds}s")


def _validate_and_record_outcome(
    case: M29EvidenceCase,
    task_data: dict[str, Any],
) -> M29CaseOutcome:
    """Enforce invariants on terminal task and produce M29CaseOutcome."""
    status = task_data.get("status")
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"expected terminal status, got {status}")

    # Orchestration and runtime assertions
    orch = str(task_data.get("orchestration_runtime") or "")
    if orch != "temporal":
        raise ValueError(f"task {task_data.get('task_id')} runtime '{orch}' != 'temporal'")

    mode = str(task_data.get("runtime_mode") or "")
    if mode != "native_agent":
        raise ValueError(f"task {task_data.get('task_id')} mode '{mode}' != 'native_agent'")

    spec = task_data.get("task_spec") or {}
    task_type = spec.get("task_type")
    if task_type != case.task_class:
        raise ValueError(
            f"task {task_data.get('task_id')} class '{task_type}' != '{case.task_class}'"
        )

    profile = task_data.get("chosen_profile")
    if profile != case.worker_profile:
        raise ValueError(
            f"task {task_data.get('task_id')} profile '{profile}' != '{case.worker_profile}'"
        )

    changed_files = _get_changed_files_count(task_data)
    if changed_files > 0:
        raise ValueError(
            f"task {task_data.get('task_id')} modified {changed_files} files (must be 0)"
        )

    created = task_data.get("created_at") or datetime.now(UTC).isoformat()
    updated = task_data.get("updated_at") or created
    try:
        c_dt = datetime.fromisoformat(created)
        u_dt = datetime.fromisoformat(updated)
        elapsed = max(0.0, (u_dt - c_dt).total_seconds())
    except Exception:
        elapsed = None

    failure_kind = _resolve_task_failure_kind(task_data)
    return M29CaseOutcome(
        case_id=case.case_id,
        task_id=str(task_data.get("task_id")),
        task_class=case.task_class,
        worker_profile=case.worker_profile,
        terminal_status="completed" if status == "completed" else "failed",
        failure_kind=failure_kind,
        files_changed_count=changed_files,
        time_to_terminal_seconds=elapsed,
        created_at=created,
        terminal_at=updated,
        orchestration_runtime="temporal",
        runtime_mode="native_agent",
        has_unresolved_interactions=False,
        gate_failures=[],
    )


def init_cmd(args: argparse.Namespace) -> int:
    """Initialize a private evidence bundle directory."""
    bundle_path = _bundle_path(args.bundle_dir)
    if bundle_path.exists():
        print(f"Error: bundle already exists at {bundle_path}", file=sys.stderr)
        return 1

    if not args.ack_live_read_only_evidence:
        print(
            "Error: must pass --ack-live-read-only-evidence to confirm executing "
            "read-only tasks against live repository and persistent database.",
            file=sys.stderr,
        )
        return 1

    suite = _load_suite(args.suite_path)
    suite_sha256 = _calculate_sha256(args.suite_path)
    identity = M29BundleIdentity(
        build_sha=args.build_sha,
        target_repository_revision=args.target_repository_revision,
        environment=args.environment,
        operator=args.operator,
    )
    bundle = M29EvidenceBundle(
        identity=identity,
        suite_sha256=suite_sha256,
        in_flight={},
        cases={},
    )
    _save_bundle(args.bundle_dir, bundle)
    print(f"Initialized M29 evidence bundle at {args.bundle_dir}")
    print(f"  Suite cases: {len(suite.cases)}")
    print(f"  Build SHA: {identity.build_sha}")
    print(f"  Target repository revision: {identity.target_repository_revision}")
    return 0


def status_cmd(args: argparse.Namespace) -> int:
    """Print current progress and status of evidence collection."""
    bundle = _load_bundle(args.bundle_dir)
    suite = _load_suite(args.suite_path)

    completed = len(bundle.cases)
    in_flight = len(bundle.in_flight)
    remaining = TOTAL_CASES - completed

    print(f"M29 Evidence Bundle Status: {args.bundle_dir}")
    print(f"  Build SHA: {bundle.identity.build_sha}")
    print(f"  Target repo revision: {bundle.identity.target_repository_revision}")
    print(f"  Created at: {bundle.identity.created_at.isoformat()}")
    print(
        f"  Progress: {completed}/{TOTAL_CASES} completed, "
        f"{in_flight} in-flight, {remaining} pending"
    )

    by_cell: dict[tuple[str, str], list[str]] = {}
    for outcome in bundle.cases.values():
        cell = (outcome.task_class, outcome.worker_profile)
        by_cell.setdefault(cell, []).append(outcome.terminal_status)

    print("\nCompleted Cell Outcomes:")
    for (task_class, profile), statuses in sorted(by_cell.items()):
        comp = sum(1 for s in statuses if s == "completed")
        fail = sum(1 for s in statuses if s == "failed")
        print(
            f"  {task_class:15} | {profile:38} : {len(statuses):2} tasks "
            f"({comp} completed, {fail} failed)"
        )

    if remaining > 0:
        print("\nPending Cases:")
        for c in suite.cases:
            if c.case_id not in bundle.cases:
                in_f = (
                    f" (in-flight: {bundle.in_flight[c.case_id]})"
                    if c.case_id in bundle.in_flight
                    else ""
                )
                print(f"  - {c.case_id:42} [{c.task_class:13} | {c.worker_profile}]{in_f}")
    return 0


def run_batch_cmd(args: argparse.Namespace) -> int:
    """Execute all remaining cases sequentially with automatic resumption."""
    bundle = _load_bundle(args.bundle_dir)
    suite = _load_suite(args.suite_path)

    actual_sha = _calculate_sha256(args.suite_path)
    if bundle.suite_sha256 != actual_sha:
        raise ValueError(
            f"bundle suite hash {bundle.suite_sha256} does not match "
            f"{args.suite_path} ({actual_sha})"
        )

    bundle_id = bundle.identity.created_at.isoformat()

    with _client(args.base_url, args.api_token_env) as client:
        print(f"Performing preflight checks at {args.base_url}...")
        _check_stack_health(client)
        print("Stack is healthy and execution-ready.")

        for i, case in enumerate(suite.cases, 1):
            if case.case_id in bundle.cases:
                LOGGER.info(
                    "[%d/%d] Skipping already completed case %s", i, TOTAL_CASES, case.case_id
                )
                continue

            # Check if task was already submitted and in-flight
            task_id = bundle.in_flight.get(case.case_id)
            if task_id:
                print(
                    f"[{i}/{TOTAL_CASES}] Resuming in-flight task {task_id} "
                    f"for case {case.case_id}..."
                )
            else:
                print(
                    f"[{i}/{TOTAL_CASES}] Submitting case {case.case_id} "
                    f"({case.task_class}, {case.worker_profile})..."
                )
                task_id = _submit_case(client, case, bundle_id, args)
                bundle.in_flight[case.case_id] = task_id
                _save_bundle(args.bundle_dir, bundle)

            print(f"  Polling task {task_id} (timeout={args.timeout_seconds}s)...")
            task_data = _poll_task(client, task_id, args.timeout_seconds)

            outcome = _validate_and_record_outcome(case, task_data)
            bundle.cases[case.case_id] = outcome
            bundle.in_flight.pop(case.case_id, None)
            _save_bundle(args.bundle_dir, bundle)

            status_str = outcome.terminal_status.upper()
            duration_str = (
                f"{outcome.time_to_terminal_seconds:.1f}s"
                if outcome.time_to_terminal_seconds
                else "n/a"
            )
            print(f"  -> Case {case.case_id} finished: {status_str} in {duration_str}")

    print(f"\nAll {TOTAL_CASES} cases in evidence wave completed successfully!")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser with init, status, and run-batch commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    init = subparsers.add_parser("init", help="Initialize new private evidence bundle")
    init.add_argument("--bundle-dir", type=Path, required=True, help="Path to bundle directory")
    init.add_argument("--build-sha", required=True, help="Committed harness revision SHA")
    init.add_argument(
        "--target-repository-revision", required=True, help="Pinned origin/master revision SHA"
    )
    init.add_argument("--environment", default="local", help="Deployment environment name")
    init.add_argument(
        "--operator", default=os.getenv("USER") or "operator", help="Operator identifier"
    )
    init.add_argument(
        "--ack-live-read-only-evidence",
        action="store_true",
        dest="ack_live_read_only_evidence",
        help="Acknowledge executing read-only tasks against live repo and persistent DB",
    )
    init.add_argument(
        "--suite-path",
        type=Path,
        default=Path("evaluation/m29_live_provider_suite.json"),
        help="Path to frozen suite JSON",
    )

    # status
    status = subparsers.add_parser("status", help="Show bundle progress and status")
    status.add_argument("--bundle-dir", type=Path, required=True, help="Path to bundle directory")
    status.add_argument(
        "--suite-path",
        type=Path,
        default=Path("evaluation/m29_live_provider_suite.json"),
        help="Path to frozen suite JSON",
    )

    # run-batch
    batch = subparsers.add_parser("run-batch", help="Run or resume remaining cases in batch")
    batch.add_argument("--bundle-dir", type=Path, required=True, help="Path to bundle directory")
    batch.add_argument("--base-url", default="http://127.0.0.1:8000", help="API base URL")
    batch.add_argument(
        "--api-token-env", default="CODE_AGENT_API_SHARED_SECRET", help="API secret env var"
    )
    batch.add_argument("--repo-key", default="code-agent", help="Allowed repository key")
    batch.add_argument("--branch", default="master", help="Target branch name")
    batch.add_argument(
        "--timeout-seconds", type=int, default=900, help="Per-task polling timeout in seconds"
    )
    batch.add_argument(
        "--suite-path",
        type=Path,
        default=Path("evaluation/m29_live_provider_suite.json"),
        help="Path to frozen suite JSON",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return init_cmd(args)
    if args.command == "status":
        return status_cmd(args)
    if args.command == "run-batch":
        return run_batch_cmd(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
