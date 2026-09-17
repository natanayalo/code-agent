"""Unit tests for M29 provider reliability robustness analysis."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from evaluation.provider_reliability_extractor import (
    ExtractedTaskEvidence,
    build_evidence_cells,
)
from evaluation.provider_reliability_models import (
    MutationMode,
    ProviderReliabilityRobustnessPolicy,
    ReliabilityReportPolicy,
    TaskExclusionSummary,
)
from evaluation.provider_reliability_recommendation import generate_recommendations
from evaluation.provider_reliability_robustness import (
    evaluate_robustness_snapshot,
    partition_temporal_cohort_tasks,
    partition_window_tasks,
    run_group_bootstrap,
)
from evaluation.provider_reliability_robustness_report import (
    assert_sanitized_robustness_report,
    render_json_robustness_report,
    render_markdown_robustness_report,
)

AS_OF = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


def _make_evidence(
    terminal_ts: datetime,
    task_class: str = "feature",
    profile: str = "codex-native-executor",
    mode: MutationMode = "mutation",
    accepted: bool = True,
    duration: float | None = 45.0,
) -> ExtractedTaskEvidence:
    """Helper to create minimal valid ExtractedTaskEvidence."""
    return ExtractedTaskEvidence(
        task_class=task_class,
        profile=profile,
        mutation_mode=mode,
        accepted=accepted,
        terminal_timestamp=terminal_ts,
        duration_seconds=duration,
        failure_kind=None if accepted else "test_failure",
        verifier_repaired=False,
        review_repaired=False,
        clarification_count=0,
        approval_count=0,
        has_intervention=False,
        has_override=False,
        has_budget=True,
        dispatched=True,
        execution_success=accepted,
        verification_applicable=True,
        verification_passed=accepted,
        review_applicable=False,
        review_passed=False,
        delivery_applicable=False,
        delivery_passed=False,
    )


def test_window_boundary_filtering() -> None:
    """Validate task filtering across 30, 60, and 90-day lookback windows."""
    t_inside_30 = _make_evidence(AS_OF - timedelta(days=15))
    t_exact_30 = _make_evidence(AS_OF - timedelta(days=30))
    t_inside_60 = _make_evidence(AS_OF - timedelta(days=45))
    t_exact_60 = _make_evidence(AS_OF - timedelta(days=60))
    t_inside_90 = _make_evidence(AS_OF - timedelta(days=75))
    t_exact_90 = _make_evidence(AS_OF - timedelta(days=90))
    t_outside = _make_evidence(AS_OF - timedelta(days=91))

    all_tasks = [
        t_inside_30,
        t_exact_30,
        t_inside_60,
        t_exact_60,
        t_inside_90,
        t_exact_90,
        t_outside,
    ]

    w30 = partition_window_tasks(all_tasks, AS_OF, 30)
    assert t_inside_30 in w30
    assert t_exact_30 in w30
    assert t_inside_60 not in w30
    assert len(w30) == 2

    w60 = partition_window_tasks(all_tasks, AS_OF, 60)
    assert t_inside_30 in w60
    assert t_exact_30 in w60
    assert t_inside_60 in w60
    assert t_exact_60 in w60
    assert t_inside_90 not in w60
    assert len(w60) == 4

    w90 = partition_window_tasks(all_tasks, AS_OF, 90)
    assert len(w90) == 6
    assert t_outside not in w90


def test_temporal_cohort_split_non_overlapping() -> None:
    """Validate strict temporal half-split with zero overlap at the 45-day boundary."""
    t_hist_edge = _make_evidence(AS_OF - timedelta(days=90))
    t_hist_mid = _make_evidence(AS_OF - timedelta(days=60))
    t_hist_just_before = _make_evidence(AS_OF - timedelta(days=45, microseconds=1))
    t_boundary_exact = _make_evidence(AS_OF - timedelta(days=45))
    t_rec_mid = _make_evidence(AS_OF - timedelta(days=20))
    t_rec_edge = _make_evidence(AS_OF)

    all_tasks = [
        t_hist_edge,
        t_hist_mid,
        t_hist_just_before,
        t_boundary_exact,
        t_rec_mid,
        t_rec_edge,
    ]

    hist, rec = partition_temporal_cohort_tasks(all_tasks, AS_OF, split_days=45, lookback_days=90)

    assert t_hist_edge in hist
    assert t_hist_mid in hist
    assert t_hist_just_before in hist
    assert t_boundary_exact not in hist
    assert len(hist) == 3

    assert t_boundary_exact in rec
    assert t_rec_mid in rec
    assert t_rec_edge in rec
    assert len(rec) == 3

    # Zero overlap check
    hist_set = set(id(t) for t in hist)
    rec_set = set(id(t) for t in rec)
    assert hist_set.isdisjoint(rec_set)


def test_recommendation_ranking_parity() -> None:
    """Verify ranking logic in robustness preserves exact production recommendation parity."""
    policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=AS_OF,
        window_start_at=AS_OF - timedelta(days=90),
        window_end_at=AS_OF,
    )

    # Codex: 10/10 accepted, latency 100s
    codex_tasks = [
        _make_evidence(AS_OF - timedelta(days=1), profile="codex-native-executor", duration=100.0)
        for _ in range(10)
    ]
    # Antigravity: 10/10 accepted, latency 40s (same Wilson lower bound, lower latency)
    ag_tasks = [
        _make_evidence(
            AS_OF - timedelta(days=1), profile="antigravity-native-executor", duration=40.0
        )
        for _ in range(10)
    ]

    cells = build_evidence_cells(codex_tasks + ag_tasks, policy)
    recs = generate_recommendations(cells, as_of=AS_OF)

    assert len(recs) >= 1
    feature_rec = next(
        r for r in recs if r.task_class == "feature" and r.mutation_mode == "mutation"
    )
    assert feature_rec.recommended_profile == "antigravity-native-executor"
    assert feature_rec.rankings[0].profile == "antigravity-native-executor"
    assert feature_rec.rankings[0].rank == 1
    assert feature_rec.rankings[1].profile == "codex-native-executor"
    assert feature_rec.rankings[1].rank == 2


def test_recommendation_missing_median_latency_tie_break() -> None:
    """Validate that missing median latency is treated as float('inf') matching production."""
    policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=AS_OF,
        window_start_at=AS_OF - timedelta(days=90),
        window_end_at=AS_OF,
    )

    p1_tasks = [
        _make_evidence(AS_OF - timedelta(days=1), profile="codex-native-executor", duration=None)
        for _ in range(10)
    ]
    p2_tasks = [
        _make_evidence(
            AS_OF - timedelta(days=1), profile="antigravity-native-executor", duration=50.0
        )
        for _ in range(10)
    ]

    cells = build_evidence_cells(p1_tasks + p2_tasks, policy)
    recs = generate_recommendations(cells, as_of=AS_OF)
    rec = next(r for r in recs if r.task_class == "feature" and r.mutation_mode == "mutation")
    assert rec.recommended_profile == "antigravity-native-executor"


def test_deterministic_bootstrap_same_seed_reproducibility() -> None:
    """Verify that same seed produces identical results and seed is tracked in policy."""
    codex_tasks = [
        _make_evidence(AS_OF - timedelta(days=1), profile="codex-native-executor", accepted=True)
        for _ in range(15)
    ]
    ag_tasks = [
        _make_evidence(
            AS_OF - timedelta(days=1), profile="antigravity-native-executor", accepted=(i % 2 == 0)
        )
        for i in range(15)
    ]
    profile_tasks = {
        "codex-native-executor": codex_tasks,
        "antigravity-native-executor": ag_tasks,
    }

    run1 = run_group_bootstrap(
        task_class="feature",
        mutation_mode="mutation",
        profile_tasks=profile_tasks,
        min_samples=10,
        iterations=500,
        seed=29,
    )
    run2 = run_group_bootstrap(
        task_class="feature",
        mutation_mode="mutation",
        profile_tasks=profile_tasks,
        min_samples=10,
        iterations=500,
        seed=29,
    )

    assert run1.status == "complete"
    assert run1.candidates[0].profile == run2.candidates[0].profile
    assert run1.candidates[0].win_count == run2.candidates[0].win_count
    assert run1.candidates[0].win_probability == run2.candidates[0].win_probability
    assert run1.candidates[0].rank_counts == run2.candidates[0].rank_counts


def test_bootstrap_probability_accounting_invariants() -> None:
    """Test probability accounting: candidate rank and win probabilities sum to 1.0."""
    codex_tasks = [
        _make_evidence(AS_OF - timedelta(days=1), profile="codex-native-executor", accepted=True)
        for _ in range(12)
    ]
    ag_tasks = [
        _make_evidence(
            AS_OF - timedelta(days=1), profile="antigravity-native-executor", accepted=True
        )
        for _ in range(12)
    ]
    profile_tasks = {
        "codex-native-executor": codex_tasks,
        "antigravity-native-executor": ag_tasks,
    }

    iterations = 1000
    res = run_group_bootstrap(
        task_class="feature",
        mutation_mode="mutation",
        profile_tasks=profile_tasks,
        min_samples=10,
        iterations=iterations,
        seed=29,
    )

    assert res.status == "complete"
    assert len(res.candidates) == 2

    tot_win_count = sum(c.win_count for c in res.candidates)
    assert tot_win_count == iterations

    tot_win_prob = sum(c.win_probability for c in res.candidates)
    assert abs(tot_win_prob - 1.0) < 1e-3

    for cand in res.candidates:
        cand_rank_counts_sum = sum(cand.rank_counts.values())
        assert cand_rank_counts_sum == iterations
        cand_rank_probs_sum = sum(cand.rank_probabilities.values())
        assert abs(cand_rank_probs_sum - 1.0) < 1e-3


def test_controlled_resample_dominance() -> None:
    """Test controlled resample behavior: 100% success dominates 0% success with probability 1.0."""
    p_perfect = [
        _make_evidence(AS_OF - timedelta(days=1), profile="codex-native-executor", accepted=True)
        for _ in range(10)
    ]
    p_failed = [
        _make_evidence(
            AS_OF - timedelta(days=1), profile="antigravity-native-executor", accepted=False
        )
        for _ in range(10)
    ]
    profile_tasks = {
        "codex-native-executor": p_perfect,
        "antigravity-native-executor": p_failed,
    }

    res = run_group_bootstrap(
        task_class="feature",
        mutation_mode="mutation",
        profile_tasks=profile_tasks,
        min_samples=10,
        iterations=200,
        seed=29,
    )
    assert res.status == "complete"
    winner = res.candidates[0]
    assert winner.profile == "codex-native-executor"
    assert winner.win_count == 200
    assert winner.win_probability == 1.0
    loser = res.candidates[1]
    assert loser.profile == "antigravity-native-executor"
    assert loser.win_count == 0
    assert loser.win_probability == 0.0


def test_insufficient_data_group_handling() -> None:
    """Verify that fewer than 2 eligible profiles emits insufficient_data and skips bootstrap."""
    codex_tasks = [
        _make_evidence(AS_OF - timedelta(days=1), profile="codex-native-executor", accepted=True)
        for _ in range(10)
    ]
    ag_tasks = [
        _make_evidence(
            AS_OF - timedelta(days=1), profile="antigravity-native-executor", accepted=True
        )
        for _ in range(5)  # only 5, below floor of 10
    ]
    profile_tasks = {
        "codex-native-executor": codex_tasks,
        "antigravity-native-executor": ag_tasks,
    }

    res = run_group_bootstrap(
        task_class="feature",
        mutation_mode="mutation",
        profile_tasks=profile_tasks,
        min_samples=10,
        iterations=1000,
        seed=29,
    )
    assert res.status == "insufficient_data"
    assert res.candidates == []
    assert res.eligible_profiles == ["codex-native-executor"]
    assert "insufficient_eligible_candidates" in (res.fallback_reason or "")


def test_schema_strictness_and_lookback_validation() -> None:
    """Validate policy schema strictness, rejecting extra fields and invalid lookback_days."""
    with pytest.raises(ValidationError):
        # lookback_days must be 90
        ProviderReliabilityRobustnessPolicy(
            as_of=AS_OF,
            lookback_days=30,  # type: ignore[arg-type]
            window_start_at=AS_OF - timedelta(days=30),
            window_end_at=AS_OF,
        )

    with pytest.raises(ValidationError):
        # extra field forbidden
        ProviderReliabilityRobustnessPolicy(
            as_of=AS_OF,
            lookback_days=90,
            window_start_at=AS_OF - timedelta(days=90),
            window_end_at=AS_OF,
            unknown_parameter="unexpected",  # type: ignore[call-arg]
        )


def test_sanitization_public_allowlist_validator() -> None:
    """Test allowlist validator rejects forbidden keys and unsafe substrings."""
    valid_payload = {
        "schema_version": 1,
        "generated_at": AS_OF.isoformat(),
        "status": "complete",
        "policy": {
            "schema_version": 1,
            "lookback_days": 90,
            "min_samples": 10,
            "confidence_level": 0.95,
            "as_of": AS_OF.isoformat(),
            "window_start_at": (AS_OF - timedelta(days=90)).isoformat(),
            "window_end_at": AS_OF.isoformat(),
            "windows_days": [30, 60, 90],
            "temporal_split_days": 45,
            "bootstrap_iterations": 10000,
            "bootstrap_seed": 29,
            "enabled_profiles": ["codex-native-executor"],
            "expected_groups": [["feature", "mutation"]],
        },
        "exclusions": {
            "total_tasks_scanned": 10,
            "included_tasks_count": 10,
            "excluded_tasks_count": 0,
            "by_reason": {},
        },
        "windows": [],
        "temporal_cohorts": [],
        "bootstrap_results": [],
    }

    assert_sanitized_robustness_report(valid_payload)

    # Injection of unauthorized key
    bad_key_payload = dict(valid_payload)
    bad_key_payload["task_id"] = "task_secret_123"
    with pytest.raises(ValueError, match="Forbidden key detected"):
        assert_sanitized_robustness_report(bad_key_payload)

    # Injection of unsafe path substring
    bad_val_payload = json.loads(json.dumps(valid_payload))
    bad_val_payload["windows"] = [
        {
            "window_days": 30,
            "window_start_at": (AS_OF - timedelta(days=30)).isoformat(),
            "window_end_at": AS_OF.isoformat(),
            "included_tasks_count": 1,
            "recommendations": [
                {
                    "task_class": "feature",
                    "mutation_mode": "mutation",
                    "recommended_profile": "codex-native-executor",
                    "rankings": [],
                    "fallback_reason": "Error in /Users/dev/file.py",
                }
            ],
        }
    ]
    with pytest.raises(ValueError, match="Unsafe substring"):
        assert_sanitized_robustness_report(bad_val_payload)


def test_full_report_evaluation_and_rendering() -> None:
    """Test full evaluation pipeline from ExtractedTaskEvidence to Markdown/JSON output."""
    policy = ProviderReliabilityRobustnessPolicy(
        as_of=AS_OF,
        lookback_days=90,
        window_start_at=AS_OF - timedelta(days=90),
        window_end_at=AS_OF,
        min_samples=10,
        bootstrap_iterations=500,
        bootstrap_seed=29,
    )

    tasks: list[ExtractedTaskEvidence] = []
    # Add 12 tasks for Codex (mutation) and 12 for Antigravity (mutation) distributed across 90 days
    for day in range(1, 85, 7):
        ts = AS_OF - timedelta(days=day)
        tasks.append(_make_evidence(ts, profile="codex-native-executor", accepted=True))
        tasks.append(_make_evidence(ts, profile="antigravity-native-executor", accepted=True))

    exclusions = TaskExclusionSummary(
        total_tasks_scanned=len(tasks) + 2,
        included_tasks_count=len(tasks),
        excluded_tasks_count=2,
        by_reason={"cancelled": 2},
    )

    report = evaluate_robustness_snapshot(tasks, exclusions, policy)
    assert report.status == "partial"
    assert len(report.windows) == 3
    assert len(report.temporal_cohorts) == 2
    assert len(report.bootstrap_results) >= 1

    json_str = render_json_robustness_report(report)
    assert '"schema_version": 1' in json_str

    md_str = render_markdown_robustness_report(report)
    assert "# M29 Provider Reliability Robustness Report" in md_str
    assert "## 1. Executive Robustness Summary" in md_str
    assert "## 2. Window Variation Analysis" in md_str
    assert "## 3. Temporal Split-Half Cohorts" in md_str
    assert "## 4. Bootstrap Resampling Sensitivity" in md_str
    assert "## 5. Evidence Accounting & 90-Day Snapshot Exclusions" in md_str
