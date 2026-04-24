#!/usr/bin/env python3
"""Run the deterministic frozen evaluation suite and persist a JSON report."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Literal, cast

from evaluation import (
    CaseRunResult,
    EvaluationComparison,
    EvaluationProfile,
    EvaluationReport,
    OrchestratorReplayRunner,
    ReplayRunner,
    ReviewMetrics,
    WorkerOutcome,
    compare_reports,
    default_replay_outcomes,
    evaluate_suite,
    load_frozen_suite,
    load_replay_outcomes,
    write_report,
)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
        help=("Optional concurrency cap when --parallel is enabled. " "Defaults to no cap."),
    )
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
        report = EvaluationReport(
            suite_name=report.suite_name,
            total_cases=report.total_cases,
            passed_cases=report.passed_cases,
            failed_cases=report.failed_cases,
            total_score=report.total_score,
            max_score=report.max_score,
            results=report.results,
            review_metrics=report.review_metrics,
            profile=report.profile,
            comparison=comparison,
        )
    write_report(report, args.output)
    print(
        "frozen-eval:",
        f"runner={args.runner}",
        f"suite={report.suite_name}",
        f"passed={report.passed_cases}/{report.total_cases}",
        f"score={report.total_score}/{report.max_score}",
        f"output={args.output}",
    )
    return 0 if report.passed_cases == report.total_cases else 1


def main() -> int:
    return asyncio.run(_async_main())


def _parse_optional_review_metrics(payload: dict[str, object]) -> ReviewMetrics | None:
    raw_metrics = payload.get("review_metrics")
    if not isinstance(raw_metrics, dict):
        return None
    return ReviewMetrics(
        reviewed_cases=int(raw_metrics.get("reviewed_cases", 0)),
        precision=_coerce_optional_float(raw_metrics.get("precision")),
        actionable_rate=_coerce_optional_float(raw_metrics.get("actionable_rate")),
        false_positive_rate=_coerce_optional_float(raw_metrics.get("false_positive_rate")),
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
        delta_precision=_coerce_optional_float(raw_comparison.get("delta_precision")),
        delta_actionable_rate=_coerce_optional_float(raw_comparison.get("delta_actionable_rate")),
        delta_false_positive_rate=_coerce_optional_float(
            raw_comparison.get("delta_false_positive_rate")
        ),
        delta_fix_after_review_success=_coerce_optional_float(
            raw_comparison.get("delta_fix_after_review_success")
        ),
        delta_empty_review_correctness=_coerce_optional_float(
            raw_comparison.get("delta_empty_review_correctness")
        ),
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
    return float(value)


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
                ),
            )
        )

    return EvaluationReport(
        suite_name=str(payload.get("suite_name", "baseline")),
        total_cases=int(payload.get("total_cases", 0)),
        passed_cases=int(payload.get("passed_cases", 0)),
        failed_cases=int(payload.get("failed_cases", 0)),
        total_score=int(payload.get("total_score", 0)),
        max_score=int(payload.get("max_score", 0)),
        results=tuple(parsed_results),
        review_metrics=_parse_optional_review_metrics(payload),
        profile=_parse_optional_profile(payload),
        comparison=_parse_optional_comparison(payload),
    )


def _coerce_outcome_status(value: object) -> Literal["success", "failure", "error"]:
    if value in {"success", "failure", "error"}:
        return cast(Literal["success", "failure", "error"], value)
    return "failure"


if __name__ == "__main__":
    raise SystemExit(main())
