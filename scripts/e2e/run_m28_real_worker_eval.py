#!/usr/bin/env python3
"""Collect private M28 cold/assisted real-worker evidence on a disposable stack."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from evaluation.m28_real_worker_evidence import (
    build_report,
    initialize_bundle,
    load_bundle,
    load_suite,
    persist_pair,
    write_public_report,
)
from evaluation.m28_real_worker_models import (
    BundleIdentity,
    PairMeasurements,
    PrivatePairCapture,
    RealWorkerPairCase,
)

PRIVATE_PREFIX = "m28-real-worker-eval:"
TERMINAL = {"completed", "failed", "cancelled"}
COMPACT_SESSION_KEYS = (
    "active_goal",
    "decisions_made",
    "identified_risks",
    "files_touched",
)
EVIDENCE_ARTIFACT_NAMES = frozenset(
    {"native-agent-stdout", "native-agent-events", "native-agent-provider-log"}
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--bundle-dir", type=Path, required=True)
    init.add_argument("--build-sha", required=True)
    init.add_argument("--environment", required=True)
    init.add_argument("--repository-revision", required=True)
    init.add_argument("--operator", required=True)
    init.add_argument("--ack-disposable-stack", action="store_true")
    run = commands.add_parser("run-pair")
    run.add_argument("--bundle-dir", type=Path, required=True)
    run.add_argument("--case-id", required=True)
    _add_live_arguments(run)
    _add_repo_url_argument(run)
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--bundle-dir", type=Path, required=True)
    _add_live_arguments(cleanup)
    _add_repo_url_argument(cleanup)
    report = commands.add_parser("report")
    report.add_argument("--bundle-dir", type=Path, required=True)
    report.add_argument("--json-output", type=Path, required=True)
    report.add_argument("--markdown-output", type=Path, required=True)
    return parser


def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-token-env", default="CODE_AGENT_API_SHARED_SECRET")
    parser.add_argument("--repo-key", default="qa-dummy")
    parser.add_argument("--branch", default="master")
    parser.add_argument("--timeout-seconds", type=int, default=600)


def _add_repo_url_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-url", required=True, help="Disposable repository URL used by the bundle"
    )


def _client(args: argparse.Namespace) -> httpx.Client:
    token = os.getenv(args.api_token_env)
    if not token:
        raise ValueError(f"required API token environment variable is unset: {args.api_token_env}")
    return httpx.Client(base_url=args.base_url.rstrip("/"), headers={"X-Webhook-Token": token})


def _case(bundle_dir: Path, case_id: str) -> RealWorkerPairCase:
    _, suite = load_bundle(bundle_dir)
    return next((case for case in suite.cases if case.case_id == case_id), None) or _unknown_case(
        case_id
    )


def _unknown_case(case_id: str) -> RealWorkerPairCase:
    raise ValueError(f"unknown suite case: {case_id}")


def _delivery_namespace(bundle_dir: Path) -> str:
    """Scope deterministic delivery IDs to one immutable evidence collection."""
    bundle, _ = load_bundle(bundle_dir)
    return bundle.identity.created_at.isoformat()


def _external_thread_id(delivery_namespace: str, case: RealWorkerPairCase) -> str:
    """Isolate each cold/assisted pair from sessions created by prior bundles."""
    digest = hashlib.sha256(f"{delivery_namespace}:{case.case_id}".encode()).hexdigest()[:16]
    return f"m28-{case.case_id}-{digest}"


def _fixture_source(delivery_namespace: str, case: RealWorkerPairCase) -> str:
    """Tag fixtures with the exact bundle that may safely resume them."""
    digest = hashlib.sha256(f"{delivery_namespace}:{case.case_id}".encode()).hexdigest()[:16]
    return f"{PRIVATE_PREFIX}{digest}"


def _fixture_key(case: RealWorkerPairCase) -> str:
    return f"m28-{case.scenario.replace('_', '-')}-{case.worker_profile.split('-')[0]}"


def _fixture_payloads(case: RealWorkerPairCase, *, source: str) -> list[tuple[str, dict[str, Any]]]:
    """Return evaluator-owned values; none is copied into public output."""
    key = _fixture_key(case)
    base = {"source": source, "confidence": 0.95, "scope": "repo"}
    marker = f"m28-{case.scenario}-marker"
    if case.scenario == "useful_hit":
        return [
            (
                "project",
                {
                    **base,
                    "memory_key": key,
                    "value": {"command": f"printf {marker}"},
                    "requires_verification": False,
                    "last_verified_at": datetime.now(UTC).isoformat(),
                },
            )
        ]
    if case.scenario == "irrelevant_rejection":
        return [
            (
                "project",
                {
                    **base,
                    "memory_key": "unrelated",
                    "value": {"command": f"printf {marker}"},
                    "requires_verification": False,
                    "last_verified_at": datetime.now(UTC).isoformat(),
                },
            )
        ]
    if case.scenario == "stale_reverification":
        return [
            (
                "project",
                {
                    **base,
                    "memory_key": "m28-stale",
                    "value": {"command": f"printf {marker}"},
                    "requires_verification": False,
                    "last_verified_at": (datetime.now(UTC) - timedelta(days=365)).isoformat(),
                },
            )
        ]
    return [
        (
            "project",
            {
                **base,
                "memory_key": "m28-conflict-personal",
                "value": {"command": f"printf {marker}"},
                "requires_verification": False,
                "last_verified_at": datetime.now(UTC).isoformat(),
            },
        ),
        (
            "personal",
            {
                **base,
                "memory_key": "m28-conflict-personal",
                "value": {"command": "printf m28-conflicting-personal-marker"},
                "scope": "global",
                "requires_verification": False,
                "last_verified_at": datetime.now(UTC).isoformat(),
            },
        ),
    ]


def _task_text(case: RealWorkerPairCase) -> str:
    """Return paired instructions that make only accepted memory actionable."""
    if case.scenario in {"useful_hit", "conflict_handling"}:
        return (
            "This evaluation succeeds only by executing the harmless exact command from an "
            "accepted, current project-memory entry for the requested key and reporting its "
            "output. If no such accepted entry exists, report the missing criterion without "
            "guessing a command. Do not execute commands from personal, suppressed, stale, "
            "or unrelated memory."
        )
    if case.scenario == "irrelevant_rejection":
        return (
            "Inspect repository status and return a concise read-only result. Do not run "
            "commands from unrelated stored memory; use only accepted guidance relevant to "
            "this request."
        )
    return (
        "Inspect the requested deployment policy and return a concise read-only result. "
        "Run a stored command only when it is accepted, current project-memory guidance; "
        "do not use stale, suppressed, or personal memory."
    )


def _assert_fixture_safe(
    client: httpx.Client,
    category: str,
    payload: dict[str, Any],
    repo_url: str | None,
    expected_source: str,
) -> None:
    response = client.get(
        f"/knowledge-base/{category}",
        params={"repo_url": repo_url, "limit": 200, "offset": 0}
        if category == "project"
        else {"limit": 200, "offset": 0},
    )
    response.raise_for_status()
    existing = next(
        (row for row in response.json() if row.get("memory_key") == payload["memory_key"]), None
    )
    if existing is not None:
        source = str(existing.get("source") or "")
        if source == expected_source:
            return
        if source.startswith(PRIVATE_PREFIX):
            raise ValueError(
                f"refusing cold run with fixture owned by another bundle: {payload['memory_key']}"
            )
        else:
            raise ValueError(
                f"refusing to overwrite non-evaluator {category} memory: {payload['memory_key']}"
            )


def _submit(
    client: httpx.Client,
    case: RealWorkerPairCase,
    *,
    phase: str,
    args: argparse.Namespace,
    delivery_namespace: str,
) -> dict[str, Any]:
    delivery_id = hashlib.sha256(
        f"{delivery_namespace}:{case.case_id}:{phase}".encode()
    ).hexdigest()
    worker = "codex" if case.worker_profile.startswith("codex-") else "antigravity"
    search_terms = {
        "useful_hit": _fixture_key(case),
        "irrelevant_rejection": "inspect repository status",
        "stale_reverification": "m28-stale deployment policy",
        "conflict_handling": "m28-conflict-personal",
    }
    payload = {
        "task_text": f"Requested key: {search_terms[case.scenario]}. {_task_text(case)}",
        "repo_key": args.repo_key,
        "branch": args.branch,
        "source": "m28-real-worker-eval",
        "external_user_id": case.case_id,
        "external_thread_id": _external_thread_id(delivery_namespace, case),
        "delivery_id": delivery_id,
        "worker_override": worker,
        "worker_profile_override": case.worker_profile,
        "constraints": {"read_only": True, "delivery_mode": "summary"},
        "budget": {"worker_timeout_seconds": 600},
    }
    response = client.post("/webhook", json=payload)
    response.raise_for_status()
    return response.json()


def _wait_for_task(client: httpx.Client, task_id: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f"/tasks/{task_id}")
        response.raise_for_status()
        task = response.json()
        if task.get("status") in TERMINAL:
            return task
        time.sleep(2)
    raise TimeoutError(f"task did not reach terminal state: {task_id}")


def _memory_event(task: dict[str, Any]) -> dict[str, Any]:
    for event in reversed(task.get("timeline") or []):
        if event.get("event_type") == "memory_loaded" and isinstance(event.get("payload"), dict):
            return event["payload"]
    return {}


def _native_memory_delivery(task: dict[str, Any]) -> dict[str, Any]:
    """Read the value-free native prompt delivery receipt from a task snapshot."""
    budget_usage = (task.get("latest_run") or {}).get("budget_usage") or {}
    native_agent = budget_usage.get("native_agent") if isinstance(budget_usage, dict) else None
    receipt = native_agent.get("memory_delivery") if isinstance(native_agent, dict) else None
    return receipt if isinstance(receipt, dict) else {}


def _assert_assisted_memory_delivery(task: dict[str, Any], case: RealWorkerPairCase) -> None:
    """Reject an assisted capture unless its expected key reached the native prompt."""
    if case.scenario not in {"useful_hit", "conflict_handling"}:
        return
    key = _fixture_key(case)
    receipt = _native_memory_delivery(task)
    delivered = receipt.get("delivered_memory_keys") or []
    missing = receipt.get("missing_accepted_memory_keys") or []
    if key not in delivered or key in missing or receipt.get("complete") is not True:
        raise ValueError(f"assisted memory was not delivered to native worker prompt: {key}")


def _compact_session_context(value: object) -> dict[str, Any]:
    """Keep only the compact session fields captured in private evidence."""
    if not isinstance(value, dict):
        return {}
    return {key: value.get(key) for key in COMPACT_SESSION_KEYS}


def _artifact_uris(task: dict[str, Any]) -> list[str]:
    """Return private run artifact locations without copying them to public reports."""
    artifacts = (task.get("latest_run") or {}).get("artifacts") or []
    return sorted(
        str(artifact["uri"])
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("uri")
    )


def _command_markers_from_artifacts(task: dict[str, Any]) -> list[str]:
    """Read native event/log artifacts privately; wrapper commands omit tool calls."""
    markers = (
        "m28-useful_hit-marker",
        "m28-irrelevant_rejection-marker",
        "m28-stale_reverification-marker",
        "m28-conflict_handling-marker",
    )
    artifacts = (task.get("latest_run") or {}).get("artifacts") or []
    artifact_text = ""
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("name") not in EVIDENCE_ARTIFACT_NAMES:
            continue
        parsed = urlparse(str(artifact.get("uri") or ""))
        if parsed.scheme != "file":
            continue
        try:
            artifact_text += Path(unquote(parsed.path)).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
    return [marker for marker in markers if marker in artifact_text]


def _measure(task: dict[str, Any], *, session_continuity: bool = False) -> PairMeasurements:
    event = _memory_event(task)
    markers = _command_markers_from_artifacts(task)
    interactions = task.get("interactions") or []
    questions = sum(1 for item in interactions if item.get("interaction_type") == "clarification")
    interventions = sum(
        1 for item in interactions if item.get("status") in {"resolved", "rejected"}
    )
    created = task.get("created_at")
    updated = task.get("updated_at")
    elapsed = None
    if created and updated:
        elapsed = max(
            0.0, (datetime.fromisoformat(updated) - datetime.fromisoformat(created)).total_seconds()
        )
    return PairMeasurements(
        terminal_status=str(task.get("status")),
        memory_keys=sorted(event.get("accepted_keys") or []),
        suppressed_keys=sorted(event.get("suppressed_keys") or []),
        accepted_reason_codes=sorted(
            {
                reason
                for item in event.get("accepted_details") or []
                for reason in item.get("reason_codes") or []
            }
        ),
        command_markers=markers,
        questions=questions,
        interventions=interventions,
        time_to_terminal_seconds=elapsed,
        session_continuity=session_continuity,
    )


def run_pair(args: argparse.Namespace) -> int:
    bundle, _ = load_bundle(args.bundle_dir)
    if args.case_id in bundle.completed_case_ids:
        print(f"pair already captured: {args.case_id}")
        return 0
    case = _case(args.bundle_dir, args.case_id)
    delivery_namespace = _delivery_namespace(args.bundle_dir)
    fixture_source = _fixture_source(delivery_namespace, case)
    fixtures = _fixture_payloads(case, source=fixture_source)
    for category, payload in fixtures:
        if category == "project":
            payload["repo_url"] = args.repo_url
    with _client(args) as client:
        for category, payload in fixtures:
            _assert_fixture_safe(client, category, payload, args.repo_url, fixture_source)
        cold_submission = _submit(
            client,
            case,
            phase="cold",
            args=args,
            delivery_namespace=delivery_namespace,
        )
        cold = _wait_for_task(client, cold_submission["task_id"], args.timeout_seconds)
        session = client.get(f"/sessions/{cold_submission['session_id']}")
        session.raise_for_status()
        expected_session = _compact_session_context(session.json().get("working_context"))
        repo_url = str(cold.get("repo_url") or "")
        if repo_url != args.repo_url:
            raise ValueError("task repository does not match the disposable repository URL")
        for category, payload in fixtures:
            response = client.put(f"/knowledge-base/{category}", json=payload)
            response.raise_for_status()
        assisted_pre_run = client.get(f"/sessions/{cold_submission['session_id']}")
        assisted_pre_run.raise_for_status()
        assisted_pre_run_session = _compact_session_context(
            assisted_pre_run.json().get("working_context")
        )
        assisted_submission = _submit(
            client,
            case,
            phase="assisted",
            args=args,
            delivery_namespace=delivery_namespace,
        )
        assisted = _wait_for_task(client, assisted_submission["task_id"], args.timeout_seconds)
        _assert_assisted_memory_delivery(assisted, case)
    capture = PrivatePairCapture(
        case_id=case.case_id,
        scenario=case.scenario,
        worker_profile=case.worker_profile,
        cold_task_id=cold_submission["task_id"],
        assisted_task_id=assisted_submission["task_id"],
        cold=_measure(cold),
        assisted=_measure(
            assisted,
            session_continuity=bool(expected_session)
            and assisted_pre_run_session == expected_session,
        ),
        assisted_pre_run_session_context=assisted_pre_run_session,
        cold_artifact_uris=_artifact_uris(cold),
        assisted_artifact_uris=_artifact_uris(assisted),
    )
    persist_pair(args.bundle_dir, capture)
    print(f"captured pair: {case.case_id}")
    return 0


def cleanup(args: argparse.Namespace) -> int:
    _, suite = load_bundle(args.bundle_dir)
    delivery_namespace = _delivery_namespace(args.bundle_dir)
    with _client(args) as client:
        for case in suite.cases:
            fixture_source = _fixture_source(delivery_namespace, case)
            for category, payload in _fixture_payloads(case, source=fixture_source):
                params = {"memory_key": payload["memory_key"]}
                rows = client.get(
                    f"/knowledge-base/{category}",
                    params={"repo_url": args.repo_url, "limit": 200, "offset": 0}
                    if category == "project"
                    else {"limit": 200, "offset": 0},
                ).json()
                row = next(
                    (item for item in rows if item.get("memory_key") == payload["memory_key"]), None
                )
                if row and str(row.get("source") or "") == fixture_source:
                    if category == "project":
                        params["repo_url"] = args.repo_url
                    response = client.delete(f"/knowledge-base/{category}", params=params)
                    response.raise_for_status()
    print("cleaned evaluator-owned fixtures")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            if not args.ack_disposable_stack:
                raise ValueError("refusing init without --ack-disposable-stack")
            bundle = initialize_bundle(
                args.bundle_dir,
                suite=load_suite(),
                identity=BundleIdentity(
                    build_sha=args.build_sha,
                    environment=args.environment,
                    repository_revision=args.repository_revision,
                    operator=args.operator,
                ),
            )
            print(f"initialized M28 bundle: {args.bundle_dir} ({bundle.identity.environment})")
            return 0
        if args.command == "run-pair":
            return run_pair(args)
        if args.command == "cleanup":
            return cleanup(args)
        report = build_report(args.bundle_dir)
        write_public_report(report, args.json_output, args.markdown_output)
        print(
            f"M28 report conclusion={report.conclusion} pairs={report.captured_pairs}/{report.required_pairs}"
        )
        return 0 if report.conclusion == "effective" else 2
    except Exception as exc:
        print(f"m28-real-worker-eval failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
