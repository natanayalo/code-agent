#!/usr/bin/env python3
"""Run the deterministic frozen evaluation suite and persist a JSON report."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from evaluation import (
    CaseRunResult,
    EvaluationComparison,
    EvaluationProfile,
    EvaluationReport,
    OrchestratorReplayRunner,
    ReliabilityMetrics,
    ReliabilityReport,
    ReplayRunner,
    ReviewMetrics,
    ReviewOutcome,
    WorkerOutcome,
    compare_reports,
    default_replay_outcomes,
    evaluate_suite,
    load_frozen_suite,
    load_replay_outcomes,
    write_report,
)


def _add_suite_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--suite",
        type=Path,
        default=None,
        help="Path to frozen suite JSON (defaults to evaluation/frozen_suite.json).",
    )
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="Optional JSON file mapping case ids to deterministic replay outcomes.",
    )


def _add_runner_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runner",
        choices=("replay", "orchestrator"),
        default="orchestrator",
        help=(
            "Evaluation runner mode. "
            "'orchestrator' executes through the real graph path; "
            "'replay' scores outcomes directly."
        ),
    )
    parser.add_argument(
        "--worker-override",
        choices=("codex", "gemini"),
        default="codex",
        help="Worker override passed to the orchestrator runner.",
    )


def _add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluations/frozen-suite-report.json"),
        help="Path to write the structured report JSON.",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="Run frozen-suite cases concurrently while preserving deterministic report ordering.",
    )
    parser.add_argument(
        "--max-parallel-cases",
        type=int,
        default=None,
        help=("Optional concurrency cap when --parallel is enabled. Defaults to no cap."),
    )


def _add_metadata_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--variant-label",
        type=str,
        default=None,
        help="Optional A/B variant label (for example: baseline or candidate).",
    )
    parser.add_argument(
        "--review-prompt-profile",
        type=str,
        default=None,
        help="Optional review prompt profile identifier captured in report metadata.",
    )
    parser.add_argument(
        "--reviewer-model-profile",
        type=str,
        default=None,
        help="Optional reviewer model profile identifier captured in report metadata.",
    )
    parser.add_argument(
        "--compare-to-report",
        type=Path,
        default=None,
        help="Optional baseline report JSON path for structured A/B delta computation.",
    )
    parser.add_argument(
        "--mode",
        choices=("correctness", "m20-baseline"),
        default="correctness",
        help=(
            "Evaluation mode. "
            "'correctness' (default) scores output correctness only. "
            "'m20-baseline' additionally writes evaluation/baseline_m20.0.json "
            "with M20.0 reliability aggregate metrics."
        ),
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_suite_args(parser)
    _add_runner_args(parser)
    _add_output_args(parser)
    _add_metadata_args(parser)
    return parser


async def _async_main() -> int:
    args = _build_argument_parser().parse_args()
    suite = load_frozen_suite(path=args.suite)
    outcomes = (
        load_replay_outcomes(args.replay)
        if args.replay is not None
        else default_replay_outcomes(suite.cases)
    )
    if args.runner == "orchestrator":
        runner = OrchestratorReplayRunner(
            outcomes_by_case_id=outcomes,
            worker_override=args.worker_override,
        )
    else:
        runner = ReplayRunner(outcomes)
    profile = EvaluationProfile(
        variant_label=args.variant_label,
        review_prompt_profile=args.review_prompt_profile,
        reviewer_model_profile=args.reviewer_model_profile,
    )
    report = await evaluate_suite(
        suite_name=suite.suite_name,
        cases=suite.cases,
        runner=runner,
        parallel=args.parallel,
        max_parallel_cases=args.max_parallel_cases,
        profile=profile,
    )
    if args.compare_to_report is not None:
        with args.compare_to_report.open("r", encoding="utf-8") as file:
            baseline_payload = json.load(file)
        baseline_report = _report_from_payload(baseline_payload)
        comparison = compare_reports(baseline=baseline_report, candidate=report)
        report = replace(report, comparison=comparison)
    write_report(report, args.output)
    print(
        "frozen-eval:",
        f"runner={args.runner}",
        f"suite={report.suite_name}",
        f"passed={report.passed_cases}/{report.total_cases}",
        f"score={report.total_score}/{report.max_score}",
        f"output={args.output}",
    )
    if args.mode == "m20-baseline":
        _write_m20_baseline(report)
        _print_reliability_summary(report)
    return 0 if report.passed_cases == report.total_cases else 1


def main() -> int:
    return asyncio.run(_async_main())


# ---------------------------------------------------------------------------
# M20.0 baseline helpers
# ---------------------------------------------------------------------------

_M20_BASELINE_PATH = Path(__file__).resolve().parents[2] / "evaluation" / "baseline_m20.0.json"


def _write_m20_baseline(report: EvaluationReport) -> None:
    """Persist the M20.0 reliability aggregate to evaluation/baseline_m20.0.json."""
    rr = report.reliability_report
    payload: dict[str, object] = {
        "suite_name": report.suite_name,
        "total_cases": report.total_cases,
        "reliability_report": rr.to_dict() if rr is not None else None,
    }
    _M20_BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _M20_BASELINE_PATH.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")
    print(f"m20-baseline: wrote {_M20_BASELINE_PATH}")


def _print_reliability_summary(report: EvaluationReport) -> None:
    """Print a human-readable M20.0 reliability summary to stdout."""
    rr = report.reliability_report
    if rr is None:
        print("m20-baseline: no reliability metrics available (replay runner used?)")
        return
    print("\nM20.0 Reliability Summary")
    print(f"  Cases run:                    {rr.total_cases}")
    print(f"  Needing approval:             {rr.cases_needing_approval}")
    print(f"  With validation evidence:     {rr.cases_with_validation_evidence}")
    print(f"  Needing manual log inspect:   {rr.cases_needing_manual_log_inspection}")
    print(f"  With worker failure:          {rr.cases_with_worker_failure}")
    if rr.worker_failure_kind_counts:
        print(f"  Failure kind breakdown:       {rr.worker_failure_kind_counts_dict()}")
    if rr.mean_commands_run is not None:
        print(f"  Mean commands run:            {rr.mean_commands_run:.1f}")
    if rr.mean_files_changed is not None:
        print(f"  Mean files changed:           {rr.mean_files_changed:.1f}")
    if rr.mean_friction_reports is not None:
        print(f"  Mean friction reports:        {rr.mean_friction_reports:.1f}")
    print(f"  Stage latency available:      {rr.stage_latency_available}")


def _parse_optional_review_metrics(payload: dict[str, object]) -> ReviewMetrics | None:
    raw_metrics = payload.get("review_metrics")
    if not isinstance(raw_metrics, dict):
        return None
    return ReviewMetrics(
        reviewed_cases=int(raw_metrics.get("reviewed_cases", 0)),
        precision=_coerce_optional_float(raw_metrics.get("precision")),
        actionable_rate=_coerce_optional_float(raw_metrics.get("actionable_rate")),
        false_discovery_rate=_coerce_optional_float(
            raw_metrics.get("false_discovery_rate", raw_metrics.get("false_positive_rate"))
        ),
        false_positive_rate=_coerce_optional_float(
            raw_metrics.get("false_positive_rate", raw_metrics.get("false_discovery_rate"))
        ),
        fix_after_review_success=_coerce_optional_float(
            raw_metrics.get("fix_after_review_success")
        ),
        empty_review_correctness=_coerce_optional_float(
            raw_metrics.get("empty_review_correctness")
        ),
    )


def _parse_optional_profile(payload: dict[str, object]) -> EvaluationProfile | None:
    raw_profile = payload.get("profile")
    if not isinstance(raw_profile, dict):
        return None
    return EvaluationProfile(
        variant_label=_coerce_optional_str(raw_profile.get("variant_label")),
        review_prompt_profile=_coerce_optional_str(raw_profile.get("review_prompt_profile")),
        reviewer_model_profile=_coerce_optional_str(raw_profile.get("reviewer_model_profile")),
    )


def _parse_optional_comparison(payload: dict[str, object]) -> EvaluationComparison | None:
    raw_comparison = payload.get("comparison")
    if not isinstance(raw_comparison, dict):
        return None
    return EvaluationComparison(
        baseline_variant_label=_coerce_optional_str(raw_comparison.get("baseline_variant_label")),
        candidate_variant_label=_coerce_optional_str(raw_comparison.get("candidate_variant_label")),
        delta_passed_cases=int(raw_comparison.get("delta_passed_cases", 0)),
        delta_total_score=int(raw_comparison.get("delta_total_score", 0)),
        delta_reviewed_cases=int(raw_comparison.get("delta_reviewed_cases", 0)),
        delta_precision=_coerce_optional_float(raw_comparison.get("delta_precision")),
        delta_actionable_rate=_coerce_optional_float(raw_comparison.get("delta_actionable_rate")),
        delta_false_discovery_rate=_coerce_optional_float(
            raw_comparison.get(
                "delta_false_discovery_rate",
                raw_comparison.get("delta_false_positive_rate"),
            )
        ),
        delta_false_positive_rate=_coerce_optional_float(
            raw_comparison.get(
                "delta_false_positive_rate",
                raw_comparison.get("delta_false_discovery_rate"),
            )
        ),
        delta_fix_after_review_success=_coerce_optional_float(
            raw_comparison.get("delta_fix_after_review_success")
        ),
        delta_empty_review_correctness=_coerce_optional_float(
            raw_comparison.get("delta_empty_review_correctness")
        ),
        delta_cases_with_validation_evidence=int(
            raw_comparison.get("delta_cases_with_validation_evidence", 0)
        ),
        delta_cases_needing_approval=int(raw_comparison.get("delta_cases_needing_approval", 0)),
        delta_cases_needing_manual_log_inspection=int(
            raw_comparison.get("delta_cases_needing_manual_log_inspection", 0)
        ),
        delta_cases_with_worker_failure=int(
            raw_comparison.get("delta_cases_with_worker_failure", 0)
        ),
        delta_mean_commands_run=_coerce_optional_float(
            raw_comparison.get("delta_mean_commands_run")
        ),
        delta_mean_files_changed=_coerce_optional_float(
            raw_comparison.get("delta_mean_files_changed")
        ),
        delta_mean_friction_reports=_coerce_optional_float(
            raw_comparison.get("delta_mean_friction_reports")
        ),
        delta_repair_loops_total=int(raw_comparison.get("delta_repair_loops_total", 0)),
        delta_mean_time_to_pr_seconds=_coerce_optional_float(
            raw_comparison.get("delta_mean_time_to_pr_seconds")
        ),
        delta_ci_rejection_total=int(raw_comparison.get("delta_ci_rejection_total", 0)),
        delta_review_rejection_total=int(raw_comparison.get("delta_review_rejection_total", 0)),
    )


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float | str):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _coerce_optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _coerce_non_negative_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int | float | str):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return max(parsed, 0)
    return 0


def _coerce_tuple_of_pairs_str_float(value: object) -> tuple[tuple[str, float], ...]:
    if isinstance(value, dict):
        return tuple((str(k), float(v)) for k, v in value.items() if v is not None)
    if isinstance(value, list) or isinstance(value, tuple):
        res = []
        for item in value:
            if (isinstance(item, list) or isinstance(item, tuple)) and len(item) == 2:
                res.append((str(item[0]), float(item[1])))
        return tuple(res)
    return ()


def _coerce_tuple_of_pairs_str_int(value: object) -> tuple[tuple[str, int], ...]:
    if isinstance(value, dict):
        return tuple((str(k), int(v)) for k, v in value.items() if v is not None)
    if isinstance(value, list) or isinstance(value, tuple):
        res = []
        for item in value:
            if (isinstance(item, list) or isinstance(item, tuple)) and len(item) == 2:
                res.append((str(item[0]), int(item[1])))
        return tuple(res)
    return ()


def _parse_optional_review_outcome(raw_outcome: dict[str, object]) -> ReviewOutcome | None:
    raw_review = raw_outcome.get("review")
    if not isinstance(raw_review, dict):
        return None
    return ReviewOutcome(
        findings_count=_coerce_non_negative_int(raw_review.get("findings_count")),
        actionable_findings_count=_coerce_non_negative_int(
            raw_review.get("actionable_findings_count")
        ),
        false_positive_findings_count=_coerce_non_negative_int(
            raw_review.get("false_positive_findings_count")
        ),
        fix_after_review_attempted=_coerce_optional_bool(
            raw_review.get("fix_after_review_attempted")
        ),
        fix_after_review_succeeded=_coerce_optional_bool(
            raw_review.get("fix_after_review_succeeded")
        ),
    )


def _parse_optional_reliability_metrics(payload: dict[str, object]) -> ReliabilityMetrics | None:
    raw = payload.get("reliability")
    if not isinstance(raw, dict):
        return None
    return ReliabilityMetrics(
        human_interaction_count=_coerce_non_negative_int(raw.get("human_interaction_count")),
        repeated_question_count=_coerce_non_negative_int(raw.get("repeated_question_count")),
        validation_evidence_present=_coerce_optional_bool(raw.get("validation_evidence_present"))
        or False,
        manual_log_inspection_needed=_coerce_optional_bool(raw.get("manual_log_inspection_needed"))
        or False,
        worker_status=_coerce_outcome_status(raw.get("worker_status"))
        if raw.get("worker_status")
        else None,
        worker_failure_kind=_coerce_optional_str(raw.get("worker_failure_kind")),
        next_action_hint=_coerce_optional_str(raw.get("next_action_hint")),
        friction_report_count=_coerce_non_negative_int(raw.get("friction_report_count")),
        files_changed_count=_coerce_non_negative_int(raw.get("files_changed_count")),
        commands_run_count=_coerce_non_negative_int(raw.get("commands_run_count")),
        test_results_count=_coerce_non_negative_int(raw.get("test_results_count")),
        approval_required=_coerce_optional_bool(raw.get("approval_required")) or False,
        approval_status=_coerce_optional_str(raw.get("approval_status")),
        stage_latency_seconds=_coerce_tuple_of_pairs_str_float(raw.get("stage_latency_seconds")),
        stage_latency_available=_coerce_optional_bool(raw.get("stage_latency_available")) or False,
        attempt_count=_coerce_non_negative_int(raw.get("attempt_count")),
        repair_loops_count=_coerce_non_negative_int(raw.get("repair_loops_count")),
        time_to_pr_seconds=_coerce_optional_float(raw.get("time_to_pr_seconds")),
        ci_rejection_count=_coerce_non_negative_int(raw.get("ci_rejection_count")),
        review_rejection_count=_coerce_non_negative_int(raw.get("review_rejection_count")),
        validation_failure_category=_coerce_optional_str(raw.get("validation_failure_category")),
        worker_profile=_coerce_optional_str(raw.get("worker_profile")),
        provider_failure_cause=_coerce_optional_str(raw.get("provider_failure_cause")),
    )


def _parse_optional_reliability_report(payload: dict[str, object]) -> ReliabilityReport | None:
    raw = payload.get("reliability_report")
    if not isinstance(raw, dict):
        return None
    return ReliabilityReport(
        total_cases=_coerce_non_negative_int(raw.get("total_cases")),
        cases_needing_approval=_coerce_non_negative_int(raw.get("cases_needing_approval")),
        cases_with_validation_evidence=_coerce_non_negative_int(
            raw.get("cases_with_validation_evidence")
        ),
        cases_needing_manual_log_inspection=_coerce_non_negative_int(
            raw.get("cases_needing_manual_log_inspection")
        ),
        cases_with_worker_failure=_coerce_non_negative_int(raw.get("cases_with_worker_failure")),
        worker_failure_kind_counts=_coerce_tuple_of_pairs_str_int(
            raw.get("worker_failure_kind_counts")
        ),
        mean_commands_run=_coerce_optional_float(raw.get("mean_commands_run")),
        mean_files_changed=_coerce_optional_float(raw.get("mean_files_changed")),
        mean_friction_reports=_coerce_optional_float(raw.get("mean_friction_reports")),
        repair_loops_total=_coerce_non_negative_int(raw.get("repair_loops_total")),
        mean_time_to_pr_seconds=_coerce_optional_float(raw.get("mean_time_to_pr_seconds")),
        ci_rejection_total=_coerce_non_negative_int(raw.get("ci_rejection_total")),
        review_rejection_total=_coerce_non_negative_int(raw.get("review_rejection_total")),
        validation_failure_category_counts=_coerce_tuple_of_pairs_str_int(
            raw.get("validation_failure_category_counts")
        ),
        worker_profile_success_rates=_coerce_tuple_of_pairs_str_float(
            raw.get("worker_profile_success_rates")
        ),
        provider_failure_cause_counts=_coerce_tuple_of_pairs_str_int(
            raw.get("provider_failure_cause_counts")
        ),
        stage_latency_available=_coerce_optional_bool(raw.get("stage_latency_available")) or False,
        mean_stage_latency_seconds=_coerce_tuple_of_pairs_str_float(
            raw.get("mean_stage_latency_seconds")
        ),
    )


def _report_from_payload(payload: dict[str, object]) -> EvaluationReport:
    """Best-effort parser for baseline report inputs written by this script."""
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("Baseline report is missing results[] payload.")

    parsed_results: list[CaseRunResult] = []
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            raise ValueError("Baseline report result entry must be an object.")
        raw_outcome = raw_result.get("outcome")
        if not isinstance(raw_outcome, dict):
            raise ValueError("Baseline report result is missing outcome payload.")

        parsed_results.append(
            CaseRunResult(
                case_id=str(raw_result.get("case_id", "")),
                passed=bool(raw_result.get("passed")),
                score=int(raw_result.get("score", 0)),
                max_score=int(raw_result.get("max_score", 0)),
                failures=tuple(raw_result.get("failures", [])),
                outcome=WorkerOutcome(
                    status=_coerce_outcome_status(raw_outcome.get("status")),
                    summary=str(raw_outcome.get("summary", "")),
                    files_changed=tuple(raw_outcome.get("files_changed", [])),
                    tests_passed=(
                        raw_outcome.get("tests_passed")
                        if isinstance(raw_outcome.get("tests_passed"), bool)
                        else None
                    ),
                    review=_parse_optional_review_outcome(raw_outcome),
                    reliability=_parse_optional_reliability_metrics(raw_result),
                ),
                reliability=_parse_optional_reliability_metrics(raw_result),
            )
        )

    raw_total = payload.get("total_cases")
    reported_total_cases = (
        _coerce_non_negative_int(raw_total) if raw_total is not None else len(parsed_results)
    )
    if len(parsed_results) != reported_total_cases:
        raise ValueError(
            "Baseline report total_cases does not match results length: "
            f"total_cases={reported_total_cases}, results={len(parsed_results)}"
        )

    return EvaluationReport(
        suite_name=str(payload.get("suite_name", "baseline")),
        total_cases=reported_total_cases,
        passed_cases=_coerce_non_negative_int(payload.get("passed_cases")),
        failed_cases=_coerce_non_negative_int(payload.get("failed_cases")),
        total_score=_coerce_non_negative_int(payload.get("total_score")),
        max_score=_coerce_non_negative_int(payload.get("max_score")),
        results=tuple(parsed_results),
        review_metrics=_parse_optional_review_metrics(payload),
        profile=_parse_optional_profile(payload),
        comparison=_parse_optional_comparison(payload),
        reliability_report=_parse_optional_reliability_report(payload),
    )


def _coerce_outcome_status(value: object) -> Literal["success", "failure", "error"]:
    if value in {"success", "failure", "error"}:
        return cast(Literal["success", "failure", "error"], value)
    return "failure"


if __name__ == "__main__":
    raise SystemExit(main())
