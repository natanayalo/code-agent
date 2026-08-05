#!/usr/bin/env python3
"""Incrementally initialize, capture, and report the M25.6 baseline."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from temporalio.client import WorkflowHistory

from evaluation.temporal_history_evidence import (
    analyze_temporal_history,
    canonical_json_bytes,
    fetch_temporal_history,
)
from evaluation.temporal_reliability_capture import (
    HISTORIES_DIRECTORY,
    ensure_capture_available,
    initialize_bundle,
    load_bundle,
    load_captures,
    load_suite,
    persist_capture,
    persist_reused_capture,
    read_task_evidence,
    suite_digest,
)
from evaluation.temporal_reliability_models import (
    BundleIdentity,
    CapturedCaseEvidence,
    EvidenceBundleManifest,
    OperatorAnnotations,
)
from evaluation.temporal_reliability_report import (
    build_report,
    evaluate_case_gates,
    render_markdown,
    validate_sanitized_payload,
)

LOGGER = logging.getLogger("temporal_reliability_eval")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = REPOSITORY_ROOT / "evaluation" / "m25_6_reliability_suite.json"


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"required environment variable is unset: {name}")
    return value


def _write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(f"{path.suffix}.pending")
    pending.write_text(content, encoding="utf-8")
    pending.replace(path)


def _build_sha(value: str | None) -> str:
    resolved = value or os.getenv("BUILD_SHA")
    if not resolved:
        raise ValueError("set BUILD_SHA or pass --build-sha")
    return resolved


def _handle_init(args: argparse.Namespace) -> int:
    suite = load_suite(args.suite)
    identity = BundleIdentity(
        build_sha=_build_sha(args.build_sha),
        environment=args.environment,
        operator=args.operator,
        temporal_address=args.temporal_address,
        temporal_namespace=args.temporal_namespace,
        database_url_env=args.database_url_env,
    )
    manifest = initialize_bundle(bundle_dir=args.bundle_dir, suite=suite, identity=identity)
    LOGGER.info(
        "initialized evidence bundle path=%s build_sha=%s environment=%s cases=%d",
        args.bundle_dir,
        manifest.identity.build_sha,
        manifest.identity.environment,
        len(suite.cases),
    )
    return 0


async def _capture(args: argparse.Namespace) -> int:
    manifest, suite = load_bundle(args.bundle_dir)
    case = ensure_capture_available(
        manifest,
        suite,
        case_id=args.case_id,
        task_id=args.task_id,
    )
    raw_history_path = args.bundle_dir / HISTORIES_DIRECTORY / f"{case.case_id}.json"
    if raw_history_path.exists():
        raise ValueError(f"raw history already exists for case: {case.case_id}")
    annotations = OperatorAnnotations(
        manual_log_inspection=args.manual_log_inspection == "yes",
        ci_rejection_count=args.ci_rejection_count,
        review_rejection_count=args.review_rejection_count,
        next_action=args.next_action,
        notes=args.notes,
    )
    database_url = _required_env(manifest.identity.database_url_env)
    database = read_task_evidence(database_url=database_url, task_id=args.task_id)
    temporal = await fetch_temporal_history(
        task_id=args.task_id,
        address=manifest.identity.temporal_address,
        namespace=manifest.identity.temporal_namespace,
        raw_history_path=raw_history_path,
    )
    failures = evaluate_case_gates(
        case=case,
        identity=manifest.identity,
        database=database,
        temporal=temporal,
        annotations=annotations,
    )
    capture = persist_capture(
        bundle_dir=args.bundle_dir,
        manifest=manifest,
        case=case,
        task_id=args.task_id,
        database=database,
        temporal=temporal,
        annotations=annotations,
        gate_failures=failures,
    )
    LOGGER.info(
        "captured case_id=%s task_id=%s history_events=%d gate_failures=%d",
        capture.case_id,
        capture.task_id,
        capture.temporal.event_count,
        len(capture.gate_failures),
    )
    if failures:
        LOGGER.error("capture failed evidence gates: %s", ", ".join(failures))
        return 2
    return 0


def _verify_capture_integrity(
    args: argparse.Namespace,
) -> tuple[EvidenceBundleManifest, list[CapturedCaseEvidence]]:
    manifest, suite = load_bundle(args.bundle_dir)
    captures = load_captures(args.bundle_dir, manifest)
    suite_by_id = {case.case_id: case for case in suite.cases}
    capture_case_ids = [capture.case_id for capture in captures]
    if len(capture_case_ids) != len(set(capture_case_ids)):
        raise ValueError("bundle contains duplicate captured case IDs")
    if set(capture_case_ids) != set(manifest.capture_files):
        raise ValueError("bundle capture index differs from captured case IDs")
    if len(manifest.task_ids) != len(set(manifest.task_ids)):
        raise ValueError("bundle manifest contains duplicate task IDs")
    if set(manifest.task_ids) != {capture.task_id for capture in captures}:
        raise ValueError("bundle task index differs from captured task IDs")
    for capture in captures:
        if capture.database.get("task", {}).get("id") != capture.task_id:
            raise ValueError(f"capture task evidence differs from its task ID: {capture.case_id}")
        if capture.expected != suite_by_id.get(capture.case_id):
            raise ValueError(f"capture contract differs from frozen suite: {capture.case_id}")
        raw_path = args.bundle_dir / HISTORIES_DIRECTORY / capture.temporal.raw_history_file
        raw_history = json.loads(raw_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(canonical_json_bytes(raw_history)).hexdigest()
        if digest != capture.temporal.history_sha256:
            raise ValueError(f"raw Temporal history digest mismatch: {capture.case_id}")
        current_failures = evaluate_case_gates(
            case=capture.expected,
            identity=capture.source_identity or manifest.identity,
            database=capture.database,
            temporal=capture.temporal,
            annotations=capture.annotations,
        )
        if current_failures != capture.gate_failures:
            raise ValueError(f"stored gate result is inconsistent: {capture.case_id}")
    return manifest, captures


def _handle_report(args: argparse.Namespace) -> int:
    manifest, captures = _verify_capture_integrity(args)
    report = build_report(manifest.identity, captures)
    payload = report.model_dump(mode="json")
    validate_sanitized_payload(payload)
    json_output = args.json_output or args.bundle_dir / "report.json"
    markdown_output = args.markdown_output or args.bundle_dir / "report.md"
    _write_report(json_output, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    _write_report(markdown_output, render_markdown(report))
    LOGGER.info(
        "generated report status=%s captured=%d valid=%d json=%s markdown=%s",
        report.status,
        report.captured_cases,
        report.valid_cases,
        json_output,
        markdown_output,
    )
    return 0 if report.status == "ready_for_operator_review" else 2


def _handle_reuse(args: argparse.Namespace) -> int:
    manifest, suite = load_bundle(args.bundle_dir)
    source_manifest, source_suite = load_bundle(args.source_bundle_dir)
    if suite_digest(suite) != suite_digest(source_suite):
        raise ValueError("source suite differs from destination suite")
    temporal_override = None
    gate_failures_override = None
    annotations_override = None
    if args.reanalyze_temporal_history:
        source_capture = next(
            (
                item
                for item in load_captures(args.source_bundle_dir, source_manifest)
                if item.case_id == args.case_id
            ),
            None,
        )
        if source_capture is None:
            raise ValueError(f"source bundle does not contain case: {args.case_id}")
        raw_path = (
            args.source_bundle_dir / HISTORIES_DIRECTORY / source_capture.temporal.raw_history_file
        )
        raw_history = json.loads(raw_path.read_text(encoding="utf-8"))
        workflow_history = WorkflowHistory.from_json(
            source_capture.temporal.workflow_id,
            raw_history,
        )
        temporal_override = analyze_temporal_history(
            workflow_id=source_capture.temporal.workflow_id,
            run_id=source_capture.temporal.run_id,
            workflow_status=source_capture.temporal.workflow_status,
            events=workflow_history.events,
            raw_history=raw_history,
            raw_history_file=source_capture.temporal.raw_history_file,
        )
        gate_failures_override = evaluate_case_gates(
            case=source_capture.expected,
            identity=source_capture.source_identity or source_manifest.identity,
            database=source_capture.database,
            temporal=temporal_override,
            annotations=source_capture.annotations,
        )
        if gate_failures_override:
            raise ValueError(
                "reanalyzed source capture is not valid: " + ", ".join(gate_failures_override)
            )
        note = "Reanalyzed immutable Temporal history; original failed capture was preserved."
        annotations_override = source_capture.annotations.model_copy(
            update={
                "notes": "\n".join(
                    item for item in [source_capture.annotations.notes, note] if item
                )
            }
        )
    capture = persist_reused_capture(
        bundle_dir=args.bundle_dir,
        manifest=manifest,
        suite=suite,
        source_bundle_dir=args.source_bundle_dir,
        source_manifest=source_manifest,
        case_id=args.case_id,
        temporal_override=temporal_override,
        gate_failures_override=gate_failures_override,
        annotations_override=annotations_override,
    )
    LOGGER.info(
        "reused case_id=%s source_build_sha=%s temporal_reanalyzed=%s",
        capture.case_id,
        source_manifest.identity.build_sha,
        args.reanalyze_temporal_history,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="freeze a suite and deployment identity")
    init.add_argument("--bundle-dir", type=Path, required=True)
    init.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    init.add_argument("--build-sha")
    init.add_argument("--environment", required=True)
    init.add_argument("--operator", required=True)
    init.add_argument("--database-url-env", default="DATABASE_URL")
    init.add_argument("--temporal-address", default="localhost:7233")
    init.add_argument("--temporal-namespace", default="default")

    capture = subparsers.add_parser("capture", help="capture one immutable case")
    capture.add_argument("--bundle-dir", type=Path, required=True)
    capture.add_argument("--case-id", required=True)
    capture.add_argument("--task-id", required=True)
    capture.add_argument("--manual-log-inspection", choices=("yes", "no"), required=True)
    capture.add_argument("--ci-rejection-count", type=int, required=True)
    capture.add_argument("--review-rejection-count", type=int, required=True)
    capture.add_argument("--next-action")
    capture.add_argument("--notes")

    report = subparsers.add_parser("report", help="validate and render sanitized reports")
    report.add_argument("--bundle-dir", type=Path, required=True)
    report.add_argument("--json-output", type=Path)
    report.add_argument("--markdown-output", type=Path)

    reuse = subparsers.add_parser("reuse", help="import one valid capture from a prior bundle")
    reuse.add_argument("--bundle-dir", type=Path, required=True)
    reuse.add_argument("--source-bundle-dir", type=Path, required=True)
    reuse.add_argument("--case-id", required=True)
    reuse.add_argument("--reanalyze-temporal-history", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a CLI command with concise operator-facing failure logging."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            return _handle_init(args)
        if args.command == "capture":
            return asyncio.run(_capture(args))
        if args.command == "reuse":
            return _handle_reuse(args)
        return _handle_report(args)
    except Exception as exc:  # pragma: no cover - top-level operator boundary
        LOGGER.error("%s failed: %s", args.command, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
