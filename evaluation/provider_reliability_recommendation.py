"""Candidate ranking and recommendation engine for provider reliability."""

from __future__ import annotations

from datetime import UTC, datetime

from evaluation.provider_reliability_models import (
    CandidateRanking,
    EvidenceScope,
    MutationMode,
    ProviderReliabilityEvidenceCell,
    ReportStatus,
    TaskClassRecommendation,
)


def _calculate_evidence_age_days(newest_ts: datetime | None, ref_as_of: datetime) -> float | None:
    if newest_ts is None:
        return None
    delta = ref_as_of - newest_ts
    return round(max(0.0, delta.total_seconds()) / 86400.0, 1)


def _build_candidate_ranking(
    cell: ProviderReliabilityEvidenceCell,
    ref_as_of: datetime,
    rank: int | None = None,
    is_operational: bool = False,
) -> CandidateRanking:
    age_days = _calculate_evidence_age_days(cell.newest_evidence_timestamp, ref_as_of)
    if is_operational:
        reasons = [
            "diagnostic_scope_non_comparable: operational scope aggregates "
            "heterogeneous model vintages; recommendations suppressed"
        ]
        return CandidateRanking(
            profile=cell.profile,
            is_eligible=False,
            accepted_rate_wilson_lower=cell.accepted_task_rate_ci.lower,
            median_latency_seconds=cell.terminal_latency.median_seconds,
            rank=None,
            insufficiency_reasons=reasons,
            sample_size=cell.sample_size,
            accepted_count=cell.accepted_count,
            oldest_evidence_timestamp=cell.oldest_evidence_timestamp,
            newest_evidence_timestamp=cell.newest_evidence_timestamp,
            evidence_age_days=age_days,
        )
    return CandidateRanking(
        profile=cell.profile,
        is_eligible=cell.is_eligible,
        accepted_rate_wilson_lower=cell.accepted_task_rate_ci.lower,
        median_latency_seconds=cell.terminal_latency.median_seconds,
        rank=rank,
        insufficiency_reasons=list(cell.insufficiency_reasons) if not cell.is_eligible else [],
        sample_size=cell.sample_size,
        accepted_count=cell.accepted_count,
        oldest_evidence_timestamp=cell.oldest_evidence_timestamp,
        newest_evidence_timestamp=cell.newest_evidence_timestamp,
        evidence_age_days=age_days,
    )


def _resolve_recommendation(
    eligible: list[ProviderReliabilityEvidenceCell],
) -> tuple[str | None, str | None]:
    if len(eligible) >= 2:
        return eligible[0].profile, None
    if len(eligible) == 1:
        candidate_name = eligible[0].profile
        fallback = (
            f"insufficient_eligible_candidates: only 1 eligible candidate ('{candidate_name}') "
            "available; at least 2 required"
        )
        return None, fallback
    return None, "no_eligible_candidates: all candidates lack sufficient samples"


def generate_recommendations(
    cells: list[ProviderReliabilityEvidenceCell],
    as_of: datetime | None = None,
    evidence_scope: EvidenceScope = "current_execution_cohort",
) -> list[TaskClassRecommendation]:
    """Rank candidates and generate recommendations requiring >= 2 eligible candidates."""
    ref_as_of = as_of or datetime.now(UTC)
    is_operational = evidence_scope == "operational"
    groups: dict[tuple[str, MutationMode], list[ProviderReliabilityEvidenceCell]] = {}
    for cell in cells:
        groups.setdefault((cell.task_class, cell.mutation_mode), []).append(cell)

    recommendations: list[TaskClassRecommendation] = []
    for (task_class, mode), cell_list in sorted(groups.items()):
        if is_operational:
            rankings = [
                _build_candidate_ranking(item, ref_as_of, rank=None, is_operational=True)
                for item in cell_list
            ]
            rankings.sort(key=lambda r: r.profile)
            fallback = (
                "diagnostic_scope: operational scope aggregates heterogeneous "
                "model vintages; recommendations are valid only in current_execution_cohort"
            )
            recommendations.append(
                TaskClassRecommendation(
                    task_class=task_class,
                    mutation_mode=mode,
                    recommended_profile=None,
                    rankings=rankings,
                    fallback_reason=fallback,
                )
            )
            continue

        eligible = [c for c in cell_list if c.is_eligible]
        ineligible = [c for c in cell_list if not c.is_eligible]

        eligible.sort(
            key=lambda item: (
                -item.accepted_task_rate_ci.lower,
                item.terminal_latency.median_seconds
                if item.terminal_latency.median_seconds is not None
                else float("inf"),
                item.profile,
            )
        )

        rankings = [
            _build_candidate_ranking(item, ref_as_of, rank=idx)
            for idx, item in enumerate(eligible, start=1)
        ]
        rankings.extend(
            [_build_candidate_ranking(item, ref_as_of, rank=None) for item in ineligible]
        )
        rankings.sort(key=lambda r: (0 if r.is_eligible else 1, r.rank or 999, r.profile))

        rec_profile, fallback = _resolve_recommendation(eligible)
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


def determine_report_status(
    recommendations: list[TaskClassRecommendation],
    expected_groups: (
        list[tuple[str, MutationMode]] | tuple[tuple[str, MutationMode], ...] | None
    ) = None,
    evidence_scope: EvidenceScope = "current_execution_cohort",
) -> ReportStatus:
    """Determine report status as complete, partial, insufficient_data, or diagnostic_only."""
    if evidence_scope == "operational":
        return "diagnostic_only"

    if expected_groups is not None:
        expected_set = set(expected_groups)
        recs_to_evaluate = [
            r for r in recommendations if (r.task_class, r.mutation_mode) in expected_set
        ]
        if len(recs_to_evaluate) < len(expected_set):
            rec_count = sum(1 for r in recs_to_evaluate if r.recommended_profile is not None)
            return "partial" if rec_count > 0 else "insufficient_data"
    else:
        recs_to_evaluate = recommendations

    if not recs_to_evaluate:
        return "insufficient_data"
    rec_count = sum(1 for r in recs_to_evaluate if r.recommended_profile is not None)
    if rec_count == len(recs_to_evaluate):
        return "complete"
    if rec_count > 0:
        return "partial"
    return "insufficient_data"
