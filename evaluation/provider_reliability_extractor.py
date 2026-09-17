"""Read-only Postgres extractor and statistical engine for M29 provider reliability."""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, load_only, selectinload

from db.enums import (
    HumanInteractionType,
    OrchestrationRuntime,
    TaskStatus,
    WorkerRuntimeMode,
)
from db.models import HumanInteraction, Task, TaskTimelineEvent, WorkerRun
from evaluation.provider_reliability_models import (
    BudgetCoverageMetrics,
    InterventionMetrics,
    LatencyMetrics,
    MutationMode,
    ProfileCoverageSummary,
    ProviderReliabilityEvidenceCell,
    ProviderReliabilityReport,
    ReliabilityReportPolicy,
    RepairMetrics,
    StageOutcomeRates,
    TaskExclusionSummary,
    WilsonConfidenceInterval,
)
from evaluation.provider_reliability_recommendation import (
    determine_report_status,
    generate_recommendations,
)
from evaluation.provider_reliability_stages import (
    check_delivery_stage,
    check_review_stage,
    check_task_stages,
    check_verification_stage,
    determine_task_mutation_mode,
    is_profile_compatible_with_mode,
    resolve_failure_kind,
    verify_timeline_consistency,
)
from repositories import create_engine_from_url

LOGGER = logging.getLogger("provider_reliability_extractor")


@dataclass(frozen=True)
class ExtractedTaskEvidence:
    """Internal task-level observation with zero private user/repo content."""

    task_class: str
    profile: str
    mutation_mode: MutationMode
    accepted: bool
    terminal_timestamp: datetime
    duration_seconds: float | None
    failure_kind: str | None
    verifier_repaired: bool
    review_repaired: bool
    clarification_count: int
    approval_count: int
    has_intervention: bool
    has_override: bool
    has_budget: bool
    dispatched: bool
    execution_success: bool
    verification_applicable: bool
    verification_passed: bool
    review_applicable: bool
    review_passed: bool
    delivery_applicable: bool
    delivery_passed: bool


def compute_wilson_interval(
    k: int, n: int, confidence_level: float = 0.95
) -> WilsonConfidenceInterval:
    """Compute the two-sided Wilson score confidence interval."""
    if n <= 0:
        return WilsonConfidenceInterval(lower=0.0, center=0.0, upper=0.0)
    alpha = (1.0 + confidence_level) / 2.0
    z = statistics.NormalDist().inv_cdf(alpha)
    p = k / n
    denom = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denom
    spread = (z / denom) * math.sqrt((p * (1.0 - p) / n) + (z * z) / (4.0 * n * n))
    lower = max(0.0, center - spread)
    upper = min(1.0, center + spread)
    return WilsonConfidenceInterval(
        lower=round(lower, 4),
        center=round(center, 4),
        upper=round(upper, 4),
    )


def _calculate_latencies(values: list[float]) -> LatencyMetrics:
    """Compute summary statistics for task latencies in seconds."""
    if not values:
        return LatencyMetrics()
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    p90_idx = min(n - 1, int(math.ceil(0.9 * n)) - 1)
    return LatencyMetrics(
        median_seconds=round(statistics.median(sorted_vals), 2),
        mean_seconds=round(statistics.mean(sorted_vals), 2),
        min_seconds=round(sorted_vals[0], 2),
        max_seconds=round(sorted_vals[-1], 2),
        p90_seconds=round(sorted_vals[p90_idx], 2),
    )


def _validate_task_candidate(
    task: Task, policy: ReliabilityReportPolicy
) -> tuple[str | None, MutationMode | None, datetime | None, str | None]:
    """Check task eligibility criteria and return exclusion reason if invalid."""
    if task.status == TaskStatus.CANCELLED:
        return "cancelled", None, None, None
    if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
        return "incomplete", None, None, None
    if task.orchestration_runtime != OrchestrationRuntime.TEMPORAL:
        return "non_temporal_runtime", None, None, None
    if task.runtime_mode != WorkerRuntimeMode.NATIVE_AGENT:
        return "non_native_agent_mode", None, None, None

    if not task.task_spec or not isinstance(task.task_spec, dict):
        return "malformed_missing_task_spec", None, None, None
    if not task.task_spec.get("task_type") or not isinstance(task.task_spec.get("task_type"), str):
        return "malformed_missing_task_spec", None, None, None

    run_profiles = {r.worker_profile for r in task.worker_runs if r.worker_profile}
    if len(run_profiles) > 1:
        return "mixed_profile_execution", None, None, None

    profile = next(iter(run_profiles)) if run_profiles else task.chosen_profile
    if not profile:
        return "malformed_missing_profile", None, None, None

    mode = determine_task_mutation_mode(task.task_spec, task.constraints, profile)
    if not mode:
        return "malformed_missing_mode", None, None, None
    if not is_profile_compatible_with_mode(profile, mode):
        return "incompatible_profile_mode", None, None, None

    consistent, terminal_ts = verify_timeline_consistency(
        task.status, task.timeline_events, task.updated_at
    )
    if not consistent or terminal_ts is None:
        return "malformed_inconsistent_timeline", None, None, None

    if terminal_ts.tzinfo is None:
        terminal_ts = terminal_ts.replace(tzinfo=UTC)
    if not (policy.window_start_at <= terminal_ts <= policy.window_end_at):
        return "outside_window", None, None, None

    return None, mode, terminal_ts, profile


def _extract_single_task(
    task: Task, policy: ReliabilityReportPolicy
) -> tuple[ExtractedTaskEvidence | None, str | None]:
    """Extract and validate one task, returning evidence or an exclusion reason."""
    reason, mode, terminal_ts, profile = _validate_task_candidate(task, policy)
    if reason or not mode or not terminal_ts or not profile:
        return None, reason

    runs = sorted(
        task.worker_runs,
        key=lambda r: (r.started_at or datetime.min.replace(tzinfo=UTC), r.id or ""),
    )

    task_class = str(task.task_spec["task_type"])  # type: ignore[index]
    accepted = task.status == TaskStatus.COMPLETED
    c = task.constraints or {}

    interactions = task.human_interactions or []
    clarifications = sum(
        1 for i in interactions if i.interaction_type == HumanInteractionType.CLARIFICATION
    )
    approvals = sum(
        1
        for i in interactions
        if i.interaction_type
        in (
            HumanInteractionType.PERMISSION,
            HumanInteractionType.REVIEW,
            HumanInteractionType.MERGE,
        )
    )

    has_override = bool(
        task.worker_override is not None
        or c.get("worker_override") is not None
        or c.get("worker_profile_override") is not None
        or task.route_reason in ("manual_override", "manual_profile_override")
    )

    start_ts = task.created_at
    if start_ts and start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=UTC)
    duration = max(0.0, (terminal_ts - start_ts).total_seconds()) if start_ts else None

    stages = check_task_stages(task, runs)

    return (
        ExtractedTaskEvidence(
            task_class=task_class,
            profile=profile,
            mutation_mode=mode,
            accepted=accepted,
            terminal_timestamp=terminal_ts,
            duration_seconds=duration,
            failure_kind=resolve_failure_kind(task, accepted, runs),
            verifier_repaired=c.get("independent_verifier_repair_passes_used", 0) > 0,
            review_repaired=c.get("independent_review_repair_passes_used", 0) > 0,
            clarification_count=clarifications,
            approval_count=approvals,
            has_intervention=bool(interactions),
            has_override=has_override,
            has_budget=any(bool(r.budget_usage) for r in runs),
            dispatched=stages[0],
            execution_success=stages[1],
            verification_applicable=stages[2],
            verification_passed=stages[3],
            review_applicable=stages[4],
            review_passed=stages[5],
            delivery_applicable=stages[6],
            delivery_passed=stages[7],
        ),
        None,
    )


def _query_boundary_counts(
    session: Session, window_start_at: datetime, window_end_at: datetime
) -> tuple[int, int]:
    """Count tasks strictly outside the window boundaries."""
    older_count = (
        session.scalar(select(func.count(Task.id)).where(Task.updated_at < window_start_at)) or 0
    )
    newer_count = (
        session.scalar(select(func.count(Task.id)).where(Task.created_at > window_end_at)) or 0
    )
    return older_count, newer_count


def _build_tasks_query(window_start_at: datetime, window_end_at: datetime):
    """Build bounded SQL query with narrow column loading."""
    return (
        select(Task)
        .where(Task.created_at <= window_end_at, Task.updated_at >= window_start_at)
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
                Task.task_spec,
                Task.worker_override,
                Task.route_reason,
            ),
            selectinload(Task.worker_runs).options(
                load_only(
                    WorkerRun.id,
                    WorkerRun.task_id,
                    WorkerRun.worker_profile,
                    WorkerRun.worker_type,
                    WorkerRun.runtime_mode,
                    WorkerRun.orchestration_runtime,
                    WorkerRun.started_at,
                    WorkerRun.finished_at,
                    WorkerRun.status,
                    WorkerRun.verifier_outcome,
                    WorkerRun.budget_usage,
                    WorkerRun.artifact_index,
                    WorkerRun.delivery_metadata,
                )
            ),
            selectinload(Task.timeline_events).options(
                load_only(
                    TaskTimelineEvent.id,
                    TaskTimelineEvent.task_id,
                    TaskTimelineEvent.event_type,
                    TaskTimelineEvent.created_at,
                    TaskTimelineEvent.payload,
                )
            ),
            selectinload(Task.human_interactions).options(
                load_only(
                    HumanInteraction.id,
                    HumanInteraction.task_id,
                    HumanInteraction.interaction_type,
                    HumanInteraction.status,
                    HumanInteraction.created_at,
                )
            ),
        )
        .order_by(Task.created_at.asc())
    )


def load_and_classify_tasks(
    session: Session, policy: ReliabilityReportPolicy
) -> tuple[list[ExtractedTaskEvidence], TaskExclusionSummary]:
    """Query tasks within bounded window and classify into evidence or exclusions."""
    older_count, newer_count = _query_boundary_counts(
        session, policy.window_start_at, policy.window_end_at
    )
    tasks = (
        session.execute(_build_tasks_query(policy.window_start_at, policy.window_end_at))
        .scalars()
        .all()
    )

    included: list[ExtractedTaskEvidence] = []
    exclusions: dict[str, int] = {}
    if older_count + newer_count > 0:
        exclusions["outside_window"] = older_count + newer_count

    for task in tasks:
        evidence, reason = _extract_single_task(task, policy)
        if reason:
            exclusions[reason] = exclusions.get(reason, 0) + 1
        elif evidence:
            included.append(evidence)

    total_scanned = len(tasks) + older_count + newer_count
    summary = TaskExclusionSummary(
        total_tasks_scanned=total_scanned,
        included_tasks_count=len(included),
        excluded_tasks_count=sum(exclusions.values()),
        by_reason=dict(sorted(exclusions.items())),
    )
    return included, summary


def _compute_stage_rates(tasks: list[ExtractedTaskEvidence], n: int) -> StageOutcomeRates:
    """Compute stage-specific rates handling inapplicable optional stages."""
    if n <= 0:
        return StageOutcomeRates(dispatch_rate=0.0, execution_success_rate=0.0)

    v_app = [t for t in tasks if t.verification_applicable]
    v_rate = (
        round(sum(1 for t in v_app if t.verification_passed) / len(v_app), 4) if v_app else None
    )

    r_app = [t for t in tasks if t.review_applicable]
    r_rate = round(sum(1 for t in r_app if t.review_passed) / len(r_app), 4) if r_app else None

    d_app = [t for t in tasks if t.delivery_applicable]
    d_rate = round(sum(1 for t in d_app if t.delivery_passed) / len(d_app), 4) if d_app else None

    return StageOutcomeRates(
        dispatch_rate=round(sum(1 for t in tasks if t.dispatched) / n, 4),
        execution_success_rate=round(sum(1 for t in tasks if t.execution_success) / n, 4),
        verification_pass_rate=v_rate,
        review_pass_rate=r_rate,
        delivery_pass_rate=d_rate,
    )


def _compute_cell_repairs_and_interventions(
    tasks: list[ExtractedTaskEvidence], n: int
) -> tuple[RepairMetrics, InterventionMetrics]:
    """Compute aggregate repair and human intervention metrics for a cell."""
    if n <= 0:
        return (
            RepairMetrics(
                verifier_repairs_count=0,
                review_repairs_count=0,
                total_repaired_tasks=0,
                repair_rate=0.0,
            ),
            InterventionMetrics(
                human_interventions_count=0,
                clarification_questions_count=0,
                approvals_count=0,
                intervention_rate=0.0,
            ),
        )

    v_repairs = sum(1 for t in tasks if t.verifier_repaired)
    r_repairs = sum(1 for t in tasks if t.review_repaired)
    tot_repaired = sum(1 for t in tasks if t.verifier_repaired or t.review_repaired)
    repairs = RepairMetrics(
        verifier_repairs_count=v_repairs,
        review_repairs_count=r_repairs,
        total_repaired_tasks=tot_repaired,
        repair_rate=round(tot_repaired / n, 4),
    )

    h_count = sum(1 for t in tasks if t.has_intervention)
    clar_count = sum(t.clarification_count for t in tasks)
    appr_count = sum(t.approval_count for t in tasks)
    interventions = InterventionMetrics(
        human_interventions_count=h_count,
        clarification_questions_count=clar_count,
        approvals_count=appr_count,
        intervention_rate=round(h_count / n, 4),
    )
    return repairs, interventions


def _aggregate_cell(
    key: tuple[str, str, MutationMode],
    tasks: list[ExtractedTaskEvidence],
    policy: ReliabilityReportPolicy,
) -> ProviderReliabilityEvidenceCell:
    """Aggregate task observations for one (task_class, profile, mutation_mode) cell."""
    task_class, profile, mode = key
    n = len(tasks)
    accepted_count = sum(1 for t in tasks if t.accepted)

    failures: dict[str, int] = {}
    for t in tasks:
        if not t.accepted and t.failure_kind:
            failures[t.failure_kind] = failures.get(t.failure_kind, 0) + 1

    repairs, interventions = _compute_cell_repairs_and_interventions(tasks, n)
    overrides = sum(1 for t in tasks if t.has_override)
    budget_count = sum(1 for t in tasks if t.has_budget)
    durations = [t.duration_seconds for t in tasks if t.duration_seconds is not None]

    is_eligible = n >= policy.min_samples
    reasons = []
    if not is_eligible:
        reasons.append(f"insufficient_sample_size: {n} tasks (minimum {policy.min_samples})")

    return ProviderReliabilityEvidenceCell(
        task_class=task_class,
        profile=profile,
        mutation_mode=mode,
        sample_size=n,
        oldest_evidence_timestamp=min((t.terminal_timestamp for t in tasks), default=None),
        newest_evidence_timestamp=max((t.terminal_timestamp for t in tasks), default=None),
        accepted_count=accepted_count,
        accepted_task_rate=round(accepted_count / n, 4) if n else 0.0,
        accepted_task_rate_ci=compute_wilson_interval(accepted_count, n, policy.confidence_level),
        failure_count=n - accepted_count,
        failure_rate=round((n - accepted_count) / n, 4) if n else 0.0,
        stage_outcome_rates=_compute_stage_rates(tasks, n),
        typed_failures=dict(sorted(failures.items())),
        repairs=repairs,
        interventions=interventions,
        manual_overrides_count=overrides,
        manual_override_rate=round(overrides / n, 4) if n else 0.0,
        terminal_latency=_calculate_latencies(durations),
        budget_field_coverage=BudgetCoverageMetrics(
            budget_reported_count=budget_count,
            budget_coverage_rate=round(budget_count / n, 4) if n else 0.0,
        ),
        is_eligible=is_eligible,
        insufficiency_reasons=reasons,
    )


def extract_provider_reliability_report(
    database_url: str, policy: ReliabilityReportPolicy
) -> ProviderReliabilityReport:
    """Execute read-only extraction and assemble the versioned report."""
    engine = create_engine_from_url(database_url)
    try:
        with Session(engine) as session:
            if engine.dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION READ ONLY"))
            tasks, exclusions = load_and_classify_tasks(session, policy)
    finally:
        engine.dispose()

    grouped: dict[tuple[str, str, MutationMode], list[ExtractedTaskEvidence]] = {}
    observed_groups: set[tuple[str, MutationMode]] = set()
    for task in tasks:
        observed_groups.add((task.task_class, task.mutation_mode))
        grouped.setdefault((task.task_class, task.profile, task.mutation_mode), []).append(task)

    target_groups = set(policy.expected_groups) | observed_groups
    for t_class, m_typed in sorted(target_groups):
        comp_profiles = [
            p for p in policy.enabled_profiles if is_profile_compatible_with_mode(p, m_typed)
        ]
        for p in comp_profiles:
            if (t_class, p, m_typed) not in grouped:
                grouped[(t_class, p, m_typed)] = []

    cells = [_aggregate_cell(key, task_list, policy) for key, task_list in sorted(grouped.items())]
    recommendations = generate_recommendations(cells, as_of=policy.as_of)

    profile_coverage: list[ProfileCoverageSummary] = []
    for p in policy.enabled_profiles:
        p_mode: MutationMode = "read_only" if p.endswith("-read-only") else "mutation"
        p_cells = [c for c in cells if c.profile == p]
        tot_samples = sum(c.sample_size for c in p_cells)
        is_elig = any(c.is_eligible for c in p_cells)
        profile_coverage.append(
            ProfileCoverageSummary(
                profile=p,
                mutation_mode=p_mode,
                has_evidence=tot_samples > 0,
                sample_size=tot_samples,
                is_eligible=is_elig,
            )
        )

    return ProviderReliabilityReport(
        schema_version=1,
        generated_at=datetime.now(UTC),
        status=determine_report_status(recommendations),
        policy=policy,
        profile_coverage=profile_coverage,
        exclusions=exclusions,
        evidence_cells=cells,
        recommendations=recommendations,
    )


__all__ = [
    "ExtractedTaskEvidence",
    "check_delivery_stage",
    "check_review_stage",
    "check_task_stages",
    "check_verification_stage",
    "compute_wilson_interval",
    "determine_report_status",
    "determine_task_mutation_mode",
    "extract_provider_reliability_report",
    "generate_recommendations",
    "is_profile_compatible_with_mode",
    "load_and_classify_tasks",
    "resolve_failure_kind",
    "verify_timeline_consistency",
]
