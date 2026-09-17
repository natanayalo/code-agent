"""Candidate ranking and recommendation engine for provider reliability."""

from __future__ import annotations

from datetime import UTC, datetime

from evaluation.provider_reliability_models import (
    CandidateRanking,
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
) -> CandidateRanking:
    age_days = _calculate_evidence_age_days(cell.newest_evidence_timestamp, ref_as_of)
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
) -> list[TaskClassRecommendation]:
    """Rank candidates and generate recommendations requiring >= 2 eligible candidates."""
    ref_as_of = as_of or datetime.now(UTC)
    groups: dict[tuple[str, MutationMode], list[ProviderReliabilityEvidenceCell]] = {}
    for cell in cells:
        groups.setdefault((cell.task_class, cell.mutation_mode), []).append(cell)

    recommendations: list[TaskClassRecommendation] = []
    for (task_class, mode), cell_list in sorted(groups.items()):
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

        rankings: list[CandidateRanking] = [
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


def determine_report_status(recommendations: list[TaskClassRecommendation]) -> ReportStatus:
    """Determine report status as complete, partial, or insufficient_data."""
    if not recommendations:
        return "insufficient_data"
    rec_count = sum(1 for r in recommendations if r.recommended_profile is not None)
    if rec_count == len(recommendations):
        return "complete"
    if rec_count > 0:
        return "partial"
    return "insufficient_data"
