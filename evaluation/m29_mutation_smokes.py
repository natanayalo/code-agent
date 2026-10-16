"""Canonical extractor-stage pass/fail/unavailable smoke verification for M29."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.enums import (
    ArtifactType,
    OrchestrationRuntime,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import Task, TaskTimelineEvent, WorkerRun
from evaluation.m29_mutation_cohort import M29_MUTATION_PROFILES
from evaluation.provider_reliability_extractor import (
    extract_provider_reliability_report,
    load_task_evidence_snapshot,
)
from evaluation.provider_reliability_models import ReliabilityReportPolicy
from repositories import create_engine_from_url

LOGGER = logging.getLogger("m29_mutation_smokes")

DEFAULT_SMOKE_AS_OF = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class SmokeTaskRecord:
    """Specification of a synthetic task used for extractor-stage smoke verification."""

    task_id: str
    profile: str
    external_outcome: str  # "pass", "fail", "unavailable"
    path_variant: str  # "ordinary", "worker_reported_failed_tests"
    initial_status: TaskStatus
    terminal_event_type: TimelineEventType


@dataclass(frozen=True)
class ExtractorStageSmokeResult:
    """Verification results proving post-terminal quality isolation from canonical extractor."""

    total_smoke_tasks: int
    candidate_profiles: tuple[str, ...]
    all_verification_inapplicable: bool
    all_review_inapplicable: bool
    report_verification_pass_rates: dict[str, float | None]
    report_review_pass_rates: dict[str, float | None]
    no_review_artifacts: bool
    status_and_events_unchanged: bool
    is_valid: bool
    violations: list[str]


_SMOKE_MATRIX_SPECS: tuple[tuple[str, str, str, TaskStatus, TimelineEventType], ...] = (
    ("ord-pass-0", "pass", "ordinary", TaskStatus.COMPLETED, TimelineEventType.TASK_COMPLETED),
    ("ord-fail-1", "fail", "ordinary", TaskStatus.COMPLETED, TimelineEventType.TASK_COMPLETED),
    (
        "ord-unavail-2",
        "unavailable",
        "ordinary",
        TaskStatus.COMPLETED,
        TimelineEventType.TASK_COMPLETED,
    ),
    (
        "shrt-pass-3",
        "pass",
        "worker_reported_failed_tests",
        TaskStatus.COMPLETED,
        TimelineEventType.TASK_COMPLETED,
    ),
    (
        "shrt-fail-4",
        "fail",
        "worker_reported_failed_tests",
        TaskStatus.FAILED,
        TimelineEventType.TASK_FAILED,
    ),
    (
        "shrt-unavail-5",
        "unavailable",
        "worker_reported_failed_tests",
        TaskStatus.COMPLETED,
        TimelineEventType.TASK_COMPLETED,
    ),
    ("ord-pass-6", "pass", "ordinary", TaskStatus.COMPLETED, TimelineEventType.TASK_COMPLETED),
    ("ord-fail-7", "fail", "ordinary", TaskStatus.FAILED, TimelineEventType.TASK_FAILED),
    (
        "shrt-pass-8",
        "pass",
        "worker_reported_failed_tests",
        TaskStatus.COMPLETED,
        TimelineEventType.TASK_COMPLETED,
    ),
    (
        "shrt-fail-9",
        "fail",
        "worker_reported_failed_tests",
        TaskStatus.COMPLETED,
        TimelineEventType.TASK_COMPLETED,
    ),
)


def _build_smoke_matrix_records(profile: str, prefix: str) -> list[SmokeTaskRecord]:
    """Build 10 representative smoke task specifications for a candidate profile."""
    return [
        SmokeTaskRecord(
            task_id=f"{prefix}-{suffix}",
            profile=profile,
            external_outcome=outcome,
            path_variant=path,
            initial_status=status,
            terminal_event_type=event,
        )
        for suffix, outcome, path, status, event in _SMOKE_MATRIX_SPECS
    ]


def build_default_smoke_records() -> list[SmokeTaskRecord]:
    """Build the standard 20-task smoke matrix covering both canonical profiles."""
    codex_records = _build_smoke_matrix_records("codex-native-executor", "smoke-codex")
    ag_records = _build_smoke_matrix_records("antigravity-native-executor", "smoke-ag")
    return codex_records + ag_records


def _create_smoke_worker_run(
    task: Task,
    spec: SmokeTaskRecord,
    created_at: datetime,
    finished_at: datetime,
) -> None:
    """Attach worker run matching profile identity and path variant."""
    run_status = (
        WorkerRunStatus.SUCCESS
        if spec.initial_status == TaskStatus.COMPLETED
        else WorkerRunStatus.FAILURE
    )

    if spec.path_variant == "worker_reported_failed_tests":
        verifier_outcome = {
            "status": "failed",
            "failure_kind": "test_failure",
            "failed_tests": ["test_mutation_behavior"],
        }
    else:
        verifier_outcome = (
            {"status": "passed"}
            if spec.initial_status == TaskStatus.COMPLETED
            else {"status": "failed", "failure_kind": "compile_error"}
        )

    model_meta: dict[str, str]
    if "codex" in spec.profile:
        model_meta = {"provider": "codex", "model": "gpt-6-luna", "reasoning_effort": "high"}
    else:
        model_meta = {
            "provider": "antigravity",
            "model": "gemini-3.8-flash",
            "reasoning_effort": "medium",
        }

    task.worker_runs.append(
        WorkerRun(
            worker_type=WorkerType.CODEX if "codex" in spec.profile else WorkerType.ANTIGRAVITY,
            worker_profile=spec.profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            orchestration_runtime=OrchestrationRuntime.TEMPORAL,
            started_at=created_at,
            finished_at=finished_at,
            status=run_status,
            verifier_outcome=verifier_outcome,
            budget_usage={"tokens": 100, "native_agent": {"model_execution": model_meta}},
            artifact_index=[],  # strictly empty of INDEPENDENT_REVIEW_RESULT
        )
    )


def _create_smoke_timeline_events(
    task: Task,
    spec: SmokeTaskRecord,
    created_at: datetime,
    finished_at: datetime,
) -> None:
    """Attach ingested, dispatched, and terminal timeline events without verification events."""
    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=0,
            event_type=TimelineEventType.TASK_INGESTED,
            created_at=created_at,
        )
    )
    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=1,
            event_type=TimelineEventType.WORKER_DISPATCHED,
            created_at=created_at + timedelta(seconds=1),
        )
    )
    payload = (
        {} if spec.initial_status == TaskStatus.COMPLETED else {"failure_kind": "compile_error"}
    )
    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=2,
            event_type=spec.terminal_event_type,
            created_at=finished_at,
            payload=payload,
        )
    )


def seed_mutation_extractor_smoke_database(
    session: Session,
    records: Sequence[SmokeTaskRecord] | None = None,
    *,
    base_time: datetime | None = None,
) -> list[SmokeTaskRecord]:
    """Seed synthetic mutation smoke tasks into disposable database session."""
    smoke_records = list(records or build_default_smoke_records())
    as_of = base_time or DEFAULT_SMOKE_AS_OF

    for idx, rec in enumerate(smoke_records):
        created_at = as_of - timedelta(days=5, minutes=idx * 10)
        finished_at = created_at + timedelta(minutes=5)
        task = Task(
            id=rec.task_id,
            session_id=f"session-{rec.task_id}",
            task_text=f"Mutation task {rec.task_id}",
            repo_url="https://github.invalid/m29/disposable-fixture",
            branch="master",
            status=rec.initial_status,
            chosen_worker=WorkerType.CODEX if "codex" in rec.profile else WorkerType.ANTIGRAVITY,
            chosen_profile=rec.profile,
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            orchestration_runtime=OrchestrationRuntime.TEMPORAL,
            task_spec={
                "task_type": "feature",
                "allowed_actions": ["modify_workspace_files"],
                "verification_commands": [],
                "delivery_mode": "workspace",
            },
            constraints={
                "skip_independent_review": True,
                "post_terminal_quality_evaluation": True,
            },
            created_at=created_at,
            updated_at=finished_at,
        )
        _create_smoke_worker_run(task, rec, created_at, finished_at)
        _create_smoke_timeline_events(task, rec, created_at, finished_at)
        session.add(task)

    session.commit()
    return smoke_records


def _check_db_task_records(
    session: Session,
    records: Sequence[SmokeTaskRecord],
    violations: list[str],
) -> tuple[bool, bool]:
    """Verify database records retain unchanged status, terminal events, and no review artifacts."""
    tasks_by_id = {
        t.id: t
        for t in session.execute(select(Task).where(Task.id.in_([r.task_id for r in records])))
        .scalars()
        .all()
    }

    no_review_artifacts = True
    status_and_events_unchanged = True

    for rec in records:
        t = tasks_by_id.get(rec.task_id)
        if t is None:
            violations.append(f"task '{rec.task_id}' not found in database")
            continue
        if t.status != rec.initial_status:
            status_and_events_unchanged = False
            msg = f"task '{rec.task_id}' status mutated: {t.status}!={rec.initial_status}"
            violations.append(msg)
        last_event = t.timeline_events[-1] if t.timeline_events else None
        if last_event is None or last_event.event_type != rec.terminal_event_type:
            status_and_events_unchanged = False
            violations.append(
                f"task '{rec.task_id}' terminal event mutated: "
                f"{getattr(last_event, 'event_type', None)}!={rec.terminal_event_type}"
            )
        for r in t.worker_runs:
            for art in r.artifact_index or []:
                if art.get("artifact_type") == ArtifactType.INDEPENDENT_REVIEW_RESULT.value:
                    no_review_artifacts = False
                    violations.append(
                        f"task '{rec.task_id}' contains INDEPENDENT_REVIEW_RESULT artifact"
                    )

    return no_review_artifacts, status_and_events_unchanged


REQUIRED_SMOKE_COMBINATIONS: frozenset[tuple[str, str, str]] = frozenset(
    (profile, outcome, path_variant)
    for profile in M29_MUTATION_PROFILES
    for outcome in ("pass", "fail", "unavailable")
    for path_variant in ("ordinary", "worker_reported_failed_tests")
)


def _verify_smoke_record_matrix(
    records: Sequence[SmokeTaskRecord],
    violations: list[str],
) -> None:
    """Verify that records cover all canonical (profile, outcome, path_variant) combinations."""
    actual_combinations = {(r.profile, r.external_outcome, r.path_variant) for r in records}
    missing = REQUIRED_SMOKE_COMBINATIONS - actual_combinations
    if missing:
        violations.append(f"missing smoke combinations: {sorted(missing)}")


def _verify_task_evidence_classification(
    records: Sequence[SmokeTaskRecord],
    task_evidence: Sequence[Any],
    violations: list[str],
) -> tuple[bool, bool]:
    """Verify exact task-ID coverage and per-task extractor classification invariants."""
    record_map = {r.task_id: r for r in records}
    evidence_task_ids = {item.task_id for item in task_evidence}
    expected_task_ids = set(record_map.keys())

    if evidence_task_ids != expected_task_ids:
        missing_ids = expected_task_ids - evidence_task_ids
        extra_ids = evidence_task_ids - expected_task_ids
        violations.append(
            f"task_id coverage mismatch: missing={sorted(missing_ids)}, extra={sorted(extra_ids)}"
        )

    for item in task_evidence:
        rec = record_map.get(item.task_id)
        if rec is None:
            continue
        if item.task_class != "feature":
            violations.append(
                f"task '{item.task_id}' has task_class='{item.task_class}', expected 'feature'"
            )
        if item.mutation_mode != "mutation":
            violations.append(
                f"task '{item.task_id}' has mutation_mode='{item.mutation_mode}', "
                "expected 'mutation'"
            )
        if item.profile != rec.profile:
            violations.append(
                f"task '{item.task_id}' has profile='{item.profile}', expected '{rec.profile}'"
            )
        expected_accepted = rec.initial_status == TaskStatus.COMPLETED
        if item.accepted != expected_accepted:
            violations.append(
                f"task '{item.task_id}' has accepted={item.accepted}, expected {expected_accepted}"
            )
        if item.verification_applicable:
            violations.append(f"task {item.task_id} has verification_applicable=True")
        if item.review_applicable:
            violations.append(f"task {item.task_id} has review_applicable=True")
        if item.delivery_applicable:
            violations.append(f"task '{item.task_id}' has delivery_applicable=True")
        if item.execution_identity_status != "verified":
            violations.append(
                f"task '{item.task_id}' has execution_identity_status="
                f"'{item.execution_identity_status}', expected 'verified'"
            )

    all_v_inapp = bool(task_evidence) and all(
        not item.verification_applicable for item in task_evidence
    )
    all_r_inapp = bool(task_evidence) and all(not item.review_applicable for item in task_evidence)
    return all_v_inapp, all_r_inapp


def _verify_evidence_cells(
    report: Any,
    records: Sequence[SmokeTaskRecord],
    violations: list[str],
) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Verify evidence cells sample sizes and unapplicable stage pass rates."""
    cells = {c.profile: c for c in getattr(report, "evidence_cells", [])}
    v_rates: dict[str, float | None] = {}
    r_rates: dict[str, float | None] = {}

    for profile in M29_MUTATION_PROFILES:
        cell = cells.get(profile)
        expected_count = sum(1 for r in records if r.profile == profile)
        if cell is None:
            violations.append(f"missing evidence cell for profile '{profile}'")
            continue
        if cell.sample_size != expected_count:
            violations.append(
                f"cell '{profile}' sample_size is {cell.sample_size}, expected {expected_count}"
            )
        v_rate = cell.stage_outcome_rates.verification_pass_rate
        r_rate = cell.stage_outcome_rates.review_pass_rate
        v_rates[profile] = v_rate
        r_rates[profile] = r_rate
        if v_rate is not None:
            violations.append(f"cell '{profile}' verification_pass_rate is {v_rate}, expected None")
        if r_rate is not None:
            violations.append(f"cell '{profile}' review_pass_rate is {r_rate}, expected None")

    return v_rates, r_rates


def _assert_invariants_and_collect_violations(
    session: Session,
    records: Sequence[SmokeTaskRecord],
    task_evidence: Sequence[Any],
    report: Any,
) -> ExtractorStageSmokeResult:
    """Verify all extractor and database invariants for smoke tasks."""
    violations: list[str] = []

    _verify_smoke_record_matrix(records, violations)
    all_v_inapp, all_r_inapp = _verify_task_evidence_classification(
        records, task_evidence, violations
    )
    v_rates, r_rates = _verify_evidence_cells(report, records, violations)

    no_review_artifacts, status_and_events_unchanged = _check_db_task_records(
        session, records, violations
    )

    return ExtractorStageSmokeResult(
        total_smoke_tasks=len(records),
        candidate_profiles=M29_MUTATION_PROFILES,
        all_verification_inapplicable=all_v_inapp,
        all_review_inapplicable=all_r_inapp,
        report_verification_pass_rates=v_rates,
        report_review_pass_rates=r_rates,
        no_review_artifacts=no_review_artifacts,
        status_and_events_unchanged=status_and_events_unchanged,
        is_valid=len(violations) == 0,
        violations=violations,
    )


def default_smoke_policy() -> ReliabilityReportPolicy:
    """Return canonical 90-day ReliabilityReportPolicy for mutation smoke verification."""
    return ReliabilityReportPolicy(
        schema_version=2,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=DEFAULT_SMOKE_AS_OF,
        window_start_at=DEFAULT_SMOKE_AS_OF - timedelta(days=90),
        window_end_at=DEFAULT_SMOKE_AS_OF,
        expected_groups=(("feature", "mutation"),),
    )


def run_m29_extractor_stage_smokes(
    db_url: str,
    records: Sequence[SmokeTaskRecord] | None = None,
    policy: ReliabilityReportPolicy | None = None,
) -> ExtractorStageSmokeResult:
    """Run canonical extractor against disposable DB and verify smoke invariants."""
    engine = create_engine_from_url(db_url)
    try:
        smoke_records = list(records or build_default_smoke_records())
        smoke_policy = policy or default_smoke_policy()

        task_evidence, _ = load_task_evidence_snapshot(db_url, smoke_policy)
        report = extract_provider_reliability_report(db_url, smoke_policy)

        with Session(engine) as session:
            return _assert_invariants_and_collect_violations(
                session=session,
                records=smoke_records,
                task_evidence=task_evidence,
                report=report,
            )
    finally:
        engine.dispose()
