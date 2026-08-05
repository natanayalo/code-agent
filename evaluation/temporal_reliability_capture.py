"""Private bundle lifecycle and read-only Postgres capture for M25.6."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session, load_only, selectinload

from db.models import ExecutionPlan, ExecutionPlanNode, Task, WorkerRun
from evaluation.temporal_history_evidence import canonical_json_bytes
from evaluation.temporal_reliability_models import (
    BundleIdentity,
    CapturedCaseEvidence,
    EvidenceBundleManifest,
    OperatorAnnotations,
    ReliabilitySuite,
    ReliabilitySuiteCase,
    TemporalHistoryEvidence,
)
from repositories import create_engine_from_url

MANIFEST_FILE = "manifest.json"
FROZEN_SUITE_FILE = "frozen_suite.json"
CASES_DIRECTORY = "cases"
HISTORIES_DIRECTORY = "temporal_histories"
_CONTROL_CONSTRAINTS = {
    "requires_approval",
    "independent_verifier_repair_passes_used",
    "independent_review_repair_passes_used",
}


def load_suite(path: Path) -> ReliabilitySuite:
    """Load and validate the frozen checked-in suite."""
    return ReliabilitySuite.model_validate_json(path.read_text(encoding="utf-8"))


def suite_digest(suite: ReliabilitySuite) -> str:
    """Return the canonical digest pinned by a bundle manifest."""
    payload = suite.model_dump(mode="json")
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _write_model(path: Path, model: BaseModel, *, exclusive: bool = False) -> None:
    payload = model.model_dump_json(indent=2) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(payload)
        path.chmod(0o600)
        return
    pending = path.with_suffix(f"{path.suffix}.pending")
    pending.write_text(payload, encoding="utf-8")
    pending.chmod(0o600)
    pending.replace(path)


def initialize_bundle(
    *, bundle_dir: Path, suite: ReliabilitySuite, identity: BundleIdentity
) -> EvidenceBundleManifest:
    """Create a new private collection bundle and pin its deployment identity."""
    bundle_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest_path = bundle_dir / MANIFEST_FILE
    if manifest_path.exists():
        raise ValueError(f"bundle is already initialized: {manifest_path}")
    (bundle_dir / CASES_DIRECTORY).mkdir(mode=0o700)
    (bundle_dir / HISTORIES_DIRECTORY).mkdir(mode=0o700)
    frozen_suite_path = bundle_dir / FROZEN_SUITE_FILE
    _write_model(frozen_suite_path, suite, exclusive=True)
    manifest = EvidenceBundleManifest(
        identity=identity,
        suite_sha256=suite_digest(suite),
    )
    _write_model(manifest_path, manifest, exclusive=True)
    return manifest


def load_bundle(bundle_dir: Path) -> tuple[EvidenceBundleManifest, ReliabilitySuite]:
    """Load a bundle and reject a changed or corrupt frozen suite."""
    manifest = EvidenceBundleManifest.model_validate_json(
        (bundle_dir / MANIFEST_FILE).read_text(encoding="utf-8")
    )
    suite = load_suite(bundle_dir / FROZEN_SUITE_FILE)
    if suite_digest(suite) != manifest.suite_sha256:
        raise ValueError("frozen suite digest does not match the bundle manifest")
    return manifest, suite


def ensure_capture_available(
    manifest: EvidenceBundleManifest, suite: ReliabilitySuite, *, case_id: str, task_id: str
) -> ReliabilitySuiteCase:
    """Resolve a case and reject duplicate case or task collection."""
    cases = {case.case_id: case for case in suite.cases}
    if case_id not in cases:
        raise ValueError(f"unknown suite case: {case_id}")
    if case_id in manifest.capture_files:
        raise ValueError(f"case already captured: {case_id}")
    if task_id in manifest.task_ids:
        raise ValueError(f"task already captured: {task_id}")
    return cases[case_id]


def _enum_value(value: object | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _worker_run_snapshot(run: WorkerRun) -> dict[str, Any]:
    verifier = dict(run.verifier_outcome or {})
    return {
        "id": run.id,
        "status": _enum_value(run.status),
        "worker_type": _enum_value(run.worker_type),
        "worker_profile": run.worker_profile,
        "runtime_mode": _enum_value(run.runtime_mode),
        "orchestration_runtime": _enum_value(run.orchestration_runtime),
        "started_at": _timestamp(run.started_at),
        "finished_at": _timestamp(run.finished_at),
        "summary": run.summary,
        "requested_permission": run.requested_permission,
        "verifier_outcome": verifier,
        "failure_kind": verifier.get("failure_kind"),
        "commands_run_count": len(run.commands_run or []),
        "files_changed_count": run.files_changed_count,
        "files_changed": run.files_changed or [],
        "artifact_index": run.artifact_index or [],
        "artifacts": [
            {
                "type": _enum_value(artifact.artifact_type),
                "name": artifact.name,
                "uri": artifact.uri,
            }
            for artifact in sorted(
                run.artifacts,
                key=lambda item: (_enum_value(item.artifact_type) or "", item.name, item.id),
            )
        ],
        "runtime_manifest": run.runtime_manifest,
        "delivery_metadata": run.delivery_metadata,
    }


def _plan_snapshot(plan: ExecutionPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "nodes": [
            {
                "node_id": node.node_id,
                "sequence_number": node.sequence_number,
                "depends_on": node.depends_on or [],
                "aggregation_role": node.aggregation_role,
                "execution_mode": node.execution_mode,
                "parallel_safe": node.parallel_safe,
                "status": _enum_value(node.status),
                "retry_count": node.retry_count,
                "failure_kind": node.failure_kind,
                "verification_outcome": node.verification_outcome,
                "attempts": [
                    {
                        "attempt_number": attempt.attempt_number,
                        "status": attempt.status,
                        "failure_kind": attempt.failure_kind,
                        "duration_ms": attempt.duration_ms,
                    }
                    for attempt in node.attempts
                ],
            }
            for node in plan.nodes
        ]
    }


def _task_snapshot(task: Task) -> dict[str, Any]:
    constraints = {
        key: task.constraints[key] for key in _CONTROL_CONSTRAINTS if key in task.constraints
    }
    return {
        "task": {
            "id": task.id,
            "status": _enum_value(task.status),
            "chosen_worker": _enum_value(task.chosen_worker),
            "chosen_profile": task.chosen_profile,
            "runtime_mode": _enum_value(task.runtime_mode),
            "orchestration_runtime": _enum_value(task.orchestration_runtime),
            "attempt_count": task.attempt_count,
            "created_at": _timestamp(task.created_at),
            "updated_at": _timestamp(task.updated_at),
            "last_error": task.last_error,
            "control_constraints": constraints,
        },
        "worker_runs": [
            _worker_run_snapshot(run)
            for run in sorted(task.worker_runs, key=lambda item: (item.started_at, item.id))
        ],
        "timeline": [
            {
                "event_type": _enum_value(event.event_type),
                "attempt_number": event.attempt_number,
                "sequence_number": event.sequence_number,
                "created_at": _timestamp(event.created_at),
                "message": event.message,
                "payload": event.payload,
            }
            for event in task.timeline_events
        ],
        "interactions": [
            {
                "interaction_type": _enum_value(interaction.interaction_type),
                "status": _enum_value(interaction.status),
                "hitl_mode": _enum_value(interaction.hitl_mode),
                "created_at": _timestamp(interaction.created_at),
                "updated_at": _timestamp(interaction.updated_at),
                "summary": interaction.summary,
                "data": interaction.data,
                "response_data": interaction.response_data,
            }
            for interaction in sorted(
                task.human_interactions,
                key=lambda item: (item.created_at, item.id),
            )
        ],
        "execution_plan": _plan_snapshot(task.execution_plan),
    }


def read_task_evidence(*, database_url: str, task_id: str) -> dict[str, Any]:
    """Read one task graph without committing or mutating persisted state."""
    engine = create_engine_from_url(database_url)
    try:
        with Session(engine) as session:
            if engine.dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION READ ONLY"))
            statement = (
                select(Task)
                .where(Task.id == task_id)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.chosen_worker,
                        Task.chosen_profile,
                        Task.runtime_mode,
                        Task.orchestration_runtime,
                        Task.attempt_count,
                        Task.created_at,
                        Task.updated_at,
                        Task.last_error,
                        Task.constraints,
                    ),
                    selectinload(Task.worker_runs).selectinload(WorkerRun.artifacts),
                    selectinload(Task.timeline_events),
                    selectinload(Task.human_interactions),
                    selectinload(Task.execution_plan)
                    .selectinload(ExecutionPlan.nodes)
                    .selectinload(ExecutionPlanNode.attempts),
                )
            )
            task = session.execute(statement).scalar_one_or_none()
            if task is None:
                raise ValueError(f"task not found in Postgres: {task_id}")
            return _task_snapshot(task)
    finally:
        engine.dispose()


def persist_capture(
    *,
    bundle_dir: Path,
    manifest: EvidenceBundleManifest,
    case: ReliabilitySuiteCase,
    task_id: str,
    database: dict[str, Any],
    temporal: TemporalHistoryEvidence,
    annotations: OperatorAnnotations,
    gate_failures: list[str],
) -> CapturedCaseEvidence:
    """Write one immutable case and atomically advance its bundle manifest."""
    if database.get("task", {}).get("id") != task_id:
        raise ValueError("captured Postgres evidence does not match the requested task ID")
    current_manifest = EvidenceBundleManifest.model_validate_json(
        (bundle_dir / MANIFEST_FILE).read_text(encoding="utf-8")
    )
    if (
        current_manifest.identity != manifest.identity
        or current_manifest.suite_sha256 != manifest.suite_sha256
    ):
        raise ValueError("bundle manifest changed during capture")
    if case.case_id in current_manifest.capture_files:
        raise ValueError(f"case already captured: {case.case_id}")
    if task_id in current_manifest.task_ids:
        raise ValueError(f"task already captured: {task_id}")
    capture = CapturedCaseEvidence(
        case_id=case.case_id,
        task_id=task_id,
        expected=case,
        database=database,
        temporal=temporal,
        annotations=annotations,
        gate_failures=gate_failures,
    )
    relative_path = f"{CASES_DIRECTORY}/{case.case_id}.json"
    _write_model(bundle_dir / relative_path, capture, exclusive=True)
    updated_manifest = current_manifest.model_copy(
        update={
            "capture_files": {
                **current_manifest.capture_files,
                case.case_id: relative_path,
            },
            "task_ids": [*current_manifest.task_ids, task_id],
        }
    )
    _write_model(bundle_dir / MANIFEST_FILE, updated_manifest)
    return capture


def load_captures(bundle_dir: Path, manifest: EvidenceBundleManifest) -> list[CapturedCaseEvidence]:
    """Load every indexed capture in deterministic case-ID order at report time."""
    return [
        CapturedCaseEvidence.model_validate_json(
            (bundle_dir / manifest.capture_files[case_id]).read_text(encoding="utf-8")
        )
        for case_id in sorted(manifest.capture_files)
    ]


def persist_reused_capture(
    *,
    bundle_dir: Path,
    manifest: EvidenceBundleManifest,
    suite: ReliabilitySuite,
    source_bundle_dir: Path,
    source_manifest: EvidenceBundleManifest,
    case_id: str,
) -> CapturedCaseEvidence:
    """Import one valid immutable capture while retaining its source deployment identity."""
    if case_id in manifest.capture_files:
        raise ValueError(f"case already captured: {case_id}")
    source_path = source_manifest.capture_files.get(case_id)
    if source_path is None:
        raise ValueError(f"source bundle does not contain case: {case_id}")
    source_capture = CapturedCaseEvidence.model_validate_json(
        (source_bundle_dir / source_path).read_text(encoding="utf-8")
    )
    if source_capture.gate_failures:
        raise ValueError(f"source capture is not valid: {case_id}")
    destination_case = next((item for item in suite.cases if item.case_id == case_id), None)
    if destination_case is None or source_capture.expected != destination_case:
        raise ValueError(f"source capture contract differs from destination suite: {case_id}")
    if source_capture.task_id in manifest.task_ids:
        raise ValueError(f"task already captured: {source_capture.task_id}")
    history_name = source_capture.temporal.raw_history_file
    source_history = source_bundle_dir / HISTORIES_DIRECTORY / history_name
    destination_history = bundle_dir / HISTORIES_DIRECTORY / history_name
    if destination_history.exists():
        raise ValueError(f"destination raw history already exists: {case_id}")
    shutil.copyfile(source_history, destination_history)
    destination_history.chmod(0o600)
    capture = source_capture.model_copy(
        update={"source_identity": source_capture.source_identity or source_manifest.identity}
    )
    relative_path = f"{CASES_DIRECTORY}/{case_id}.json"
    _write_model(bundle_dir / relative_path, capture, exclusive=True)
    updated_manifest = manifest.model_copy(
        update={
            "capture_files": {**manifest.capture_files, case_id: relative_path},
            "task_ids": [*manifest.task_ids, capture.task_id],
        }
    )
    _write_model(bundle_dir / MANIFEST_FILE, updated_manifest)
    return capture
