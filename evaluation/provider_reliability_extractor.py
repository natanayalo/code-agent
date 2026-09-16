"""Read-only Postgres extractor and statistical engine for M29 provider reliability."""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, load_only, selectinload

from db.enums import (
    HumanInteractionType,
    OrchestrationRuntime,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
)
from db.models import Task
from evaluation.provider_reliability_models import (
    BudgetCoverageMetrics,
    CandidateRanking,
    InterventionMetrics,
    LatencyMetrics,
    MutationMode,
    ProviderReliabilityEvidenceCell,
    ProviderReliabilityReport,
    ReliabilityReportPolicy,
    RepairMetrics,
    StageOutcomeRates,
    TaskClassRecommendation,
    TaskExclusionSummary,
    WilsonConfidenceInterval,
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


def is_profile_compatible_with_mode(profile: str, mode: MutationMode) -> bool:
    """Check if worker profile capabilities match the requested execution mode."""
    is_read_only_profile = profile.endswith("-read-only")
    return is_read_only_profile if mode == "read_only" else not is_read_only_profile


def determine_task_mutation_mode(
    task_spec: dict[str, Any] | None,
    constraints: dict[str, Any] | None,
    profile: str | None,
) -> MutationMode | None:
    """Determine task mutation mode using TaskSpec allowed actions and constraints."""
    if task_spec:
        if task_spec.get("task_type") == "scout":
            return "read_only"
        allowed_actions = task_spec.get("allowed_actions")
        if isinstance(allowed_actions, list) and allowed_actions:
            return "mutation" if "modify_workspace_files" in allowed_actions else "read_only"

    c = constraints or {}
    if c.get("read_only") is True or c.get("task_type") == "scout":
        return "read_only"
    if profile and profile.endswith("-read-only"):
        return "read_only"
    return "mutation"


def _verify_timeline_consistency(
    status: TaskStatus,
    timeline_events: list[Any],
    updated_at: datetime | None,
) -> tuple[bool, datetime | None]:
    """Verify terminal event consistency and return terminal timestamp."""
    completed_event = None
    failed_event = None
    cancelled_event = None

    for ev in timeline_events:
        etype = ev.event_type
        if etype == TimelineEventType.TASK_COMPLETED:
            completed_event = ev
        elif etype == TimelineEventType.TASK_FAILED:
            failed_event = ev
        elif etype == TimelineEventType.TASK_CANCELLED:
            cancelled_event = ev

    if status == TaskStatus.COMPLETED:
        if completed_event is None or failed_event is not None or cancelled_event is not None:
            return False, None
        return True, completed_event.created_at or updated_at
    if status == TaskStatus.FAILED:
        if failed_event is None or completed_event is not None:
            return False, None
        return True, failed_event.created_at or updated_at
    return False, None


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


def _check_task_stages(
    task: Task, accepted: bool
) -> tuple[bool, bool, bool, bool, bool, bool, bool, bool]:
    """Inspect dispatch, execution, verification, review, and delivery stage outcomes."""
    dispatched = len(task.worker_runs) > 0
    exec_success = any(r.status == WorkerRunStatus.SUCCESS for r in task.worker_runs)

    spec = task.task_spec or {}
    c = task.constraints or {}
    v_cmds = spec.get("verification_commands") or c.get("verification_commands") or []
    has_v_events = any("verification" in str(e.event_type) for e in task.timeline_events)
    v_skipped = any(
        e.event_type == TimelineEventType.VERIFICATION_SKIPPED for e in task.timeline_events
    )
    v_applicable = bool(v_cmds or has_v_events) and not v_skipped
    v_passed = accepted if v_applicable else False

    rev_applicable = bool(c.get("requires_review") or c.get("independent_review"))
    rev_passed = accepted if rev_applicable else False

    deliv_mode = spec.get("delivery_mode")
    d_applicable = deliv_mode in ("branch", "draft_pr") or any(
        "delivery" in str(e.event_type) for e in task.timeline_events
    )
    deliv_events_pass = any(
        e.event_type == TimelineEventType.DELIVERY_COMPLETED for e in task.timeline_events
    )
    d_passed = (deliv_events_pass or accepted) if d_applicable else False

    return (
        dispatched,
        exec_success,
        v_applicable,
        v_passed,
        rev_applicable,
        rev_passed,
        d_applicable,
        d_passed,
    )


def _validate_task_candidate(
    task: Task, policy: ReliabilityReportPolicy
) -> tuple[str | None, MutationMode | None, datetime | None]:
    """Check task eligibility criteria and return exclusion reason if invalid."""
    if task.status == TaskStatus.CANCELLED:
        return "cancelled", None, None
    if task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS):
        return "incomplete", None, None
    if task.orchestration_runtime != OrchestrationRuntime.TEMPORAL:
        return "non_temporal_runtime", None, None
    if task.runtime_mode != WorkerRuntimeMode.NATIVE_AGENT:
        return "non_native_agent_mode", None, None

    if not task.task_spec or not isinstance(task.task_spec, dict):
        return "malformed_missing_task_spec", None, None
    if not task.task_spec.get("task_type") or not isinstance(task.task_spec.get("task_type"), str):
        return "malformed_missing_task_spec", None, None
    if not task.chosen_profile:
        return "malformed_missing_profile", None, None

    mode = determine_task_mutation_mode(task.task_spec, task.constraints, task.chosen_profile)
    if not mode:
        return "malformed_missing_mode", None, None
    if not is_profile_compatible_with_mode(task.chosen_profile, mode):
        return "incompatible_profile_mode", None, None

    consistent, terminal_ts = _verify_timeline_consistency(
        task.status, task.timeline_events, task.updated_at
    )
    if not consistent or terminal_ts is None:
        return "malformed_inconsistent_timeline", None, None

    if terminal_ts.tzinfo is None:
        terminal_ts = terminal_ts.replace(tzinfo=UTC)
    if not (policy.window_start_at <= terminal_ts <= policy.window_end_at):
        return "outside_window", None, None

    return None, mode, terminal_ts


def _resolve_failure_kind(task: Task, accepted: bool) -> str | None:
    """Resolve typed failure cause from worker runs or error message."""
    if accepted:
        return None
    for r in reversed(task.worker_runs):
        outcome = r.verifier_outcome or {}
        if outcome.get("failure_kind"):
            return str(outcome["failure_kind"])
    if task.last_error:
        return "task_error"
    return "unknown"


def _extract_single_task(
    task: Task, policy: ReliabilityReportPolicy
) -> tuple[ExtractedTaskEvidence | None, str | None]:
    """Extract and validate one task, returning evidence or an exclusion reason."""
    reason, mode, terminal_ts = _validate_task_candidate(task, policy)
    if reason or not mode or not terminal_ts:
        return None, reason

    task_class = str(task.task_spec["task_type"])  # type: ignore[index]
    profile = str(task.chosen_profile)
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

    start_ts = task.created_at
    if start_ts and start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=UTC)
    duration = max(0.0, (terminal_ts - start_ts).total_seconds()) if start_ts else None

    stages = _check_task_stages(task, accepted)

    return (
        ExtractedTaskEvidence(
            task_class=task_class,
            profile=profile,
            mutation_mode=mode,
            accepted=accepted,
            terminal_timestamp=terminal_ts,
            duration_seconds=duration,
            failure_kind=_resolve_failure_kind(task, accepted),
            verifier_repaired=c.get("independent_verifier_repair_passes_used", 0) > 0,
            review_repaired=c.get("independent_review_repair_passes_used", 0) > 0,
            clarification_count=clarifications,
            approval_count=approvals,
            has_intervention=bool(interactions),
            has_override=bool(
                task.worker_override
                or c.get("worker_override")
                or task.route_reason == "manual_override"
            ),
            has_budget=any(bool(r.budget_usage) for r in task.worker_runs),
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


def load_and_classify_tasks(
    session: Session, policy: ReliabilityReportPolicy
) -> tuple[list[ExtractedTaskEvidence], TaskExclusionSummary]:
    """Query tasks in read-only session and classify into evidence or exclusions."""
    stmt = (
        select(Task)
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
            selectinload(Task.worker_runs),
            selectinload(Task.timeline_events),
            selectinload(Task.human_interactions),
        )
        .order_by(Task.created_at.asc())
    )
    tasks = session.execute(stmt).scalars().all()
    included: list[ExtractedTaskEvidence] = []
    exclusions: dict[str, int] = {}

    for task in tasks:
        evidence, reason = _extract_single_task(task, policy)
        if reason:
            exclusions[reason] = exclusions.get(reason, 0) + 1
        elif evidence:
            included.append(evidence)

    summary = TaskExclusionSummary(
        total_tasks_scanned=len(tasks),
        included_tasks_count=len(included),
        excluded_tasks_count=sum(exclusions.values()),
        by_reason=dict(sorted(exclusions.items())),
    )
    return included, summary


def _compute_stage_rates(tasks: list[ExtractedTaskEvidence], n: int) -> StageOutcomeRates:
    """Compute stage-specific rates handling inapplicable optional stages."""
    v_app = [t for t in tasks if t.verification_applicable]
    v_rate = (
        round(sum(1 for t in v_app if t.verification_passed) / len(v_app), 4) if v_app else None
    )

    r_app = [t for t in tasks if t.review_applicable]
    r_rate = round(sum(1 for t in r_app if t.review_passed) / len(r_app), 4) if r_app else None

    d_app = [t for t in tasks if t.delivery_applicable]
    d_rate = round(sum(1 for t in d_app if t.delivery_passed) / len(d_app), 4) if d_app else None

    return StageOutcomeRates(
        dispatch_rate=round(sum(1 for t in tasks if t.dispatched) / n, 4) if n else 0.0,
        execution_success_rate=round(sum(1 for t in tasks if t.execution_success) / n, 4)
        if n
        else 0.0,
        verification_pass_rate=v_rate,
        review_pass_rate=r_rate,
        delivery_pass_rate=d_rate,
    )


def _compute_cell_repairs_and_interventions(
    tasks: list[ExtractedTaskEvidence], n: int
) -> tuple[RepairMetrics, InterventionMetrics]:
    """Compute aggregate repair and human intervention metrics for a cell."""
    v_repairs = sum(1 for t in tasks if t.verifier_repaired)
    r_repairs = sum(1 for t in tasks if t.review_repaired)
    tot_repaired = sum(1 for t in tasks if t.verifier_repaired or t.review_repaired)
    repairs = RepairMetrics(
        verifier_repairs_count=v_repairs,
        review_repairs_count=r_repairs,
        total_repaired_tasks=tot_repaired,
        repair_rate=round(tot_repaired / n, 4) if n else 0.0,
    )

    h_count = sum(1 for t in tasks if t.has_intervention)
    clar_count = sum(t.clarification_count for t in tasks)
    appr_count = sum(t.approval_count for t in tasks)
    interventions = InterventionMetrics(
        human_interventions_count=h_count,
        clarification_questions_count=clar_count,
        approvals_count=appr_count,
        intervention_rate=round(h_count / n, 4) if n else 0.0,
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


def generate_recommendations(
    cells: list[ProviderReliabilityEvidenceCell],
) -> list[TaskClassRecommendation]:
    """Rank candidates and generate recommendations requiring >= 2 eligible candidates."""
    groups: dict[tuple[str, MutationMode], list[ProviderReliabilityEvidenceCell]] = {}
    for cell in cells:
        groups.setdefault((cell.task_class, cell.mutation_mode), []).append(cell)

    recommendations: list[TaskClassRecommendation] = []
    for (task_class, mode), cell_list in sorted(groups.items()):
        rankings: list[CandidateRanking] = []
        eligible: list[ProviderReliabilityEvidenceCell] = []

        for c in cell_list:
            if c.is_eligible:
                eligible.append(c)
            else:
                rankings.append(
                    CandidateRanking(
                        profile=c.profile,
                        is_eligible=False,
                        accepted_rate_wilson_lower=c.accepted_task_rate_ci.lower,
                        median_latency_seconds=c.terminal_latency.median_seconds,
                        rank=None,
                        insufficiency_reasons=list(c.insufficiency_reasons),
                    )
                )

        eligible.sort(
            key=lambda item: (
                -item.accepted_task_rate_ci.lower,
                item.terminal_latency.median_seconds
                if item.terminal_latency.median_seconds is not None
                else float("inf"),
                item.profile,
            )
        )

        for rank_idx, item in enumerate(eligible, start=1):
            rankings.append(
                CandidateRanking(
                    profile=item.profile,
                    is_eligible=True,
                    accepted_rate_wilson_lower=item.accepted_task_rate_ci.lower,
                    median_latency_seconds=item.terminal_latency.median_seconds,
                    rank=rank_idx,
                    insufficiency_reasons=[],
                )
            )

        rankings.sort(key=lambda r: (0 if r.is_eligible else 1, r.rank or 999, r.profile))

        rec_profile = None
        fallback = None
        if len(eligible) >= 2:
            rec_profile = eligible[0].profile
        elif len(eligible) == 1:
            candidate_name = eligible[0].profile
            fallback = (
                f"insufficient_eligible_candidates: only 1 eligible candidate ('{candidate_name}') "
                "available; at least 2 required"
            )
        else:
            fallback = "no_eligible_candidates: all candidates lack sufficient samples"

        recommendations.append(
            TaskClassRecommendation(
                task_class=task_class,
                mutation_mode=mode,
                recommended_profile=rec_profile,
                rankings=rankings,
                fallback_reason=fallback,
            )
        )

    return recommendations


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
    for task in tasks:
        grouped.setdefault((task.task_class, task.profile, task.mutation_mode), []).append(task)

    cells = [_aggregate_cell(key, task_list, policy) for key, task_list in sorted(grouped.items())]
    recommendations = generate_recommendations(cells)

    has_recommendation = any(r.recommended_profile is not None for r in recommendations)
    status = "complete" if has_recommendation else "insufficient_data"

    return ProviderReliabilityReport(
        schema_version=1,
        generated_at=datetime.now(UTC),
        status=status,
        policy=policy,
        exclusions=exclusions,
        evidence_cells=cells,
        recommendations=recommendations,
    )
