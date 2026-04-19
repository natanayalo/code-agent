#!/usr/bin/env python3
"""Run the deterministic frozen evaluation suite and persist a JSON report."""

from __future__ import annotations

import argparse
from pathlib import Path

from evaluation import (
    OrchestratorReplayRunner,
    ReplayRunner,
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
    return parser


def main() -> int:
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
    report = evaluate_suite(
        suite_name=suite.suite_name,
        cases=suite.cases,
        runner=runner,
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


if __name__ == "__main__":
    raise SystemExit(main())
