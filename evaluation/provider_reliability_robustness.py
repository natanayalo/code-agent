"""Statistical evaluation engine for M29 provider reliability robustness analysis."""

from __future__ import annotations

import hashlib
import random
import statistics
from datetime import UTC, datetime, timedelta
from typing import Literal

from evaluation.provider_reliability_extractor import (
    ExtractedTaskEvidence,
    build_evidence_cells,
    compute_wilson_interval,
    load_task_evidence_snapshot,
)
from evaluation.provider_reliability_models import (
    BootstrapCandidateResult,
    BootstrapGroupResult,
    MutationMode,
    ProviderReliabilityRobustnessPolicy,
    ProviderReliabilityRobustnessReport,
    ReliabilityReportPolicy,
    ReportStatus,
    TaskClassRecommendation,
    TaskExclusionSummary,
    TemporalCohortResult,
    WindowRecommendationResult,
)
from evaluation.provider_reliability_recommendation import (
    determine_report_status,
    generate_recommendations,
)
from evaluation.provider_reliability_stages import is_profile_compatible_with_mode


def partition_window_tasks(
    tasks: list[ExtractedTaskEvidence],
    as_of: datetime,
    window_days: int,
) -> list[ExtractedTaskEvidence]:
    """Select tasks whose terminal timestamp falls within [as_of - window_days, as_of]."""
    start_ts = as_of - timedelta(days=window_days)
    return [t for t in tasks if start_ts <= t.terminal_timestamp <= as_of]


def partition_temporal_cohort_tasks(
    tasks: list[ExtractedTaskEvidence],
    as_of: datetime,
    split_days: int = 45,
    lookback_days: int = 90,
) -> tuple[list[ExtractedTaskEvidence], list[ExtractedTaskEvidence]]:
    """Partition tasks into non-overlapping historical [as_of-90d, as_of-45d)
    and recent [as_of-45d, as_of] cohorts.
    """
    start_hist = as_of - timedelta(days=lookback_days)
    split_ts = as_of - timedelta(days=split_days)

    historical: list[ExtractedTaskEvidence] = []
    recent: list[ExtractedTaskEvidence] = []

    for t in tasks:
        if start_hist <= t.terminal_timestamp < split_ts:
            historical.append(t)
        elif split_ts <= t.terminal_timestamp <= as_of:
            recent.append(t)

    return historical, recent


def _derive_group_seed(base_seed: int, task_class: str, mutation_mode: str) -> int:
    """Derive deterministic, group-stable integer seed from base seed and group keys."""
    raw = f"{base_seed}:{task_class}:{mutation_mode}".encode()
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big")


def _run_bootstrap_iterations(
    profile_obs: dict[str, list[tuple[bool, float | None]]],
    eligible_profiles: list[str],
    iterations: int,
    confidence_level: float,
    rng: random.Random,
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Run bootstrap resampling iterations across eligible candidate profiles."""
    win_counts = {p: 0 for p in eligible_profiles}
    rank_counts = {
        p: {str(r): 0 for r in range(1, len(eligible_profiles) + 1)} for p in eligible_profiles
    }

    for _ in range(iterations):
        scored: list[tuple[float, float, str]] = []
        for p in eligible_profiles:
            obs = profile_obs[p]
            n = len(obs)
            sampled = rng.choices(obs, k=n)
            k = sum(1 for acc, _ in sampled if acc)
            wilson_lower = compute_wilson_interval(k, n, confidence_level).lower
            durations = [dur for _, dur in sampled if dur is not None]
            med_latency = statistics.median(durations) if durations else float("inf")
            scored.append((-wilson_lower, med_latency, p))

        scored.sort()
        for rank_idx, (_, _, p) in enumerate(scored, start=1):
            rank_counts[p][str(rank_idx)] += 1
            if rank_idx == 1:
                win_counts[p] += 1

    return win_counts, rank_counts


def run_group_bootstrap(
    task_class: str,
    mutation_mode: MutationMode,
    profile_tasks: dict[str, list[ExtractedTaskEvidence]],
    min_samples: int = 10,
    iterations: int = 10000,
    seed: int = 29,
    confidence_level: float = 0.95,
) -> BootstrapGroupResult:
    """Execute deterministic bootstrap resampling for one task class and mutation mode."""
    eligible_profiles = sorted(p for p, tasks in profile_tasks.items() if len(tasks) >= min_samples)

    if len(eligible_profiles) < 2:
        fallback = (
            f"insufficient_eligible_candidates: {len(eligible_profiles)} eligible profile(s) meet "
            f"the sample floor ({min_samples}); at least 2 required"
        )
        return BootstrapGroupResult(
            task_class=task_class,
            mutation_mode=mutation_mode,
            status="insufficient_data",
            eligible_profiles=eligible_profiles,
            candidates=[],
            fallback_reason=fallback,
        )

    group_seed = _derive_group_seed(seed, task_class, mutation_mode)
    rng = random.Random(group_seed)
    profile_obs = {
        p: [(t.accepted, t.duration_seconds) for t in profile_tasks[p]] for p in eligible_profiles
    }

    win_counts, rank_counts = _run_bootstrap_iterations(
        profile_obs=profile_obs,
        eligible_profiles=eligible_profiles,
        iterations=iterations,
        confidence_level=confidence_level,
        rng=rng,
    )

    candidates: list[BootstrapCandidateResult] = []
    for p in eligible_profiles:
        w_cnt = win_counts[p]
        w_prob = round(w_cnt / iterations, 4)
        r_probs = {r_str: round(cnt / iterations, 4) for r_str, cnt in rank_counts[p].items()}
        candidates.append(
            BootstrapCandidateResult(
                profile=p,
                sample_size=len(profile_tasks[p]),
                win_count=w_cnt,
                win_probability=w_prob,
                rank_counts=rank_counts[p],
                rank_probabilities=r_probs,
            )
        )

    candidates.sort(key=lambda c: (-c.win_probability, -c.win_count, c.profile))

    return BootstrapGroupResult(
        task_class=task_class,
        mutation_mode=mutation_mode,
        status="complete",
        eligible_profiles=eligible_profiles,
        candidates=candidates,
        fallback_reason=None,
    )


def _evaluate_window(
    tasks: list[ExtractedTaskEvidence],
    as_of: datetime,
    w_days: int,
    policy: ProviderReliabilityRobustnessPolicy,
) -> WindowRecommendationResult:
    """Evaluate candidate recommendations for one bounded lookback window."""
    w_tasks = partition_window_tasks(tasks, as_of, w_days)
    w_policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=w_days,
        min_samples=policy.min_samples,
        confidence_level=policy.confidence_level,
        as_of=as_of,
        window_start_at=as_of - timedelta(days=w_days),
        window_end_at=as_of,
        enabled_profiles=list(policy.enabled_profiles),
        expected_groups=list(policy.expected_groups),
    )
    cells = build_evidence_cells(w_tasks, w_policy)
    recs = generate_recommendations(cells, as_of=as_of)
    return WindowRecommendationResult(
        window_days=w_days,
        window_start_at=w_policy.window_start_at,
        window_end_at=w_policy.window_end_at,
        included_tasks_count=len(w_tasks),
        recommendations=recs,
    )


def _evaluate_cohort(
    c_name: Literal["historical", "recent"],
    c_tasks: list[ExtractedTaskEvidence],
    start_ts: datetime,
    end_ts: datetime,
    as_of: datetime,
    policy: ProviderReliabilityRobustnessPolicy,
) -> TemporalCohortResult:
    """Evaluate candidate recommendations for one temporal half-split cohort."""
    c_policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=policy.temporal_split_days,
        min_samples=policy.min_samples,
        confidence_level=policy.confidence_level,
        as_of=as_of,
        window_start_at=start_ts,
        window_end_at=end_ts,
        enabled_profiles=list(policy.enabled_profiles),
        expected_groups=list(policy.expected_groups),
    )
    cells = build_evidence_cells(c_tasks, c_policy)
    recs = generate_recommendations(cells, as_of=as_of)
    return TemporalCohortResult(
        cohort_name=c_name,
        window_start_at=start_ts,
        window_end_at=end_ts,
        included_tasks_count=len(c_tasks),
        recommendations=recs,
    )


def evaluate_robustness_snapshot(
    tasks: list[ExtractedTaskEvidence],
    exclusions: TaskExclusionSummary,
    policy: ProviderReliabilityRobustnessPolicy,
) -> ProviderReliabilityRobustnessReport:
    """Conduct full robustness analysis across windows, cohorts, and deterministic bootstrap."""
    as_of = policy.as_of

    windows: list[WindowRecommendationResult] = [
        _evaluate_window(tasks, as_of, w_days, policy) for w_days in policy.windows_days
    ]

    hist_tasks, rec_tasks = partition_temporal_cohort_tasks(
        tasks, as_of, split_days=policy.temporal_split_days, lookback_days=policy.lookback_days
    )
    temporal_cohorts: list[TemporalCohortResult] = [
        _evaluate_cohort(
            "historical",
            hist_tasks,
            as_of - timedelta(days=policy.lookback_days),
            as_of - timedelta(days=policy.temporal_split_days),
            as_of,
            policy,
        ),
        _evaluate_cohort(
            "recent",
            rec_tasks,
            as_of - timedelta(days=policy.temporal_split_days),
            as_of,
            as_of,
            policy,
        ),
    ]

    grouped_tasks: dict[tuple[str, MutationMode], dict[str, list[ExtractedTaskEvidence]]] = {}
    observed_groups: set[tuple[str, MutationMode]] = set()
    for t in tasks:
        observed_groups.add((t.task_class, t.mutation_mode))
        group = grouped_tasks.setdefault((t.task_class, t.mutation_mode), {})
        group.setdefault(t.profile, []).append(t)

    target_groups = set(policy.expected_groups) | observed_groups
    for t_class, m_typed in target_groups:
        group = grouped_tasks.setdefault((t_class, m_typed), {})
        for p in policy.enabled_profiles:
            if is_profile_compatible_with_mode(p, m_typed):
                group.setdefault(p, [])

    bootstrap_results: list[BootstrapGroupResult] = []
    for (t_class, m_mode), profile_tasks in sorted(grouped_tasks.items()):
        b_res = run_group_bootstrap(
            task_class=t_class,
            mutation_mode=m_mode,
            profile_tasks=profile_tasks,
            min_samples=policy.min_samples,
            iterations=policy.bootstrap_iterations,
            seed=policy.bootstrap_seed,
            confidence_level=policy.confidence_level,
        )
        bootstrap_results.append(b_res)

    baseline_90d_recs: list[TaskClassRecommendation] = []
    for w in windows:
        if w.window_days == 90:
            baseline_90d_recs = w.recommendations
            break
    report_status: ReportStatus = determine_report_status(baseline_90d_recs)

    return ProviderReliabilityRobustnessReport(
        schema_version=1,
        generated_at=datetime.now(UTC),
        status=report_status,
        policy=policy,
        exclusions=exclusions,
        windows=windows,
        temporal_cohorts=temporal_cohorts,
        bootstrap_results=bootstrap_results,
    )


def extract_provider_reliability_robustness_report(
    database_url: str,
    policy: ProviderReliabilityRobustnessPolicy,
) -> ProviderReliabilityRobustnessReport:
    """Extract a 90-day task observation snapshot and compute the full robustness report."""
    tasks, exclusions = load_task_evidence_snapshot(database_url, policy)
    return evaluate_robustness_snapshot(tasks, exclusions, policy)


__all__ = [
    "evaluate_robustness_snapshot",
    "extract_provider_reliability_robustness_report",
    "partition_temporal_cohort_tasks",
    "partition_window_tasks",
    "run_group_bootstrap",
]
