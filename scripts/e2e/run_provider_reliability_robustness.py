#!/usr/bin/env python3
"""Operator CLI to generate the M29 Provider Reliability Robustness Advisory Report."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evaluation.provider_reliability_models import ProviderReliabilityRobustnessPolicy
from evaluation.provider_reliability_robustness import (
    extract_provider_reliability_robustness_report,
)
from evaluation.provider_reliability_robustness_report import (
    render_json_robustness_report,
    render_markdown_robustness_report,
)

LOGGER = logging.getLogger("run_provider_reliability_robustness")


def _write_atomic(path: Path, content: str) -> None:
    """Write text atomically to a path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(f"{path.suffix}.pending")
    pending.write_text(content, encoding="utf-8")
    pending.replace(path)


def _parse_as_of(val: str | None, max_future_skew_seconds: int = 60) -> datetime:
    """Parse as-of timestamp into UTC datetime, rejecting materially future timestamps."""
    now_utc = datetime.now(UTC)
    if not val:
        return now_utc
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            parsed = dt.replace(tzinfo=UTC)
        else:
            parsed = dt.astimezone(UTC)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO timestamp for --as-of: {val}") from exc

    if parsed > now_utc + timedelta(seconds=max_future_skew_seconds):
        raise ValueError(
            f"--as-of timestamp cannot be materially in the future (got {parsed.isoformat()} > "
            f"current {now_utc.isoformat()} + {max_future_skew_seconds}s tolerance)"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI arguments parser for robustness evaluation."""
    parser = argparse.ArgumentParser(
        description="Generate offline M29 provider reliability robustness advisory report."
    )
    parser.add_argument(
        "--database-url-env",
        required=True,
        help="Environment variable name that stores the database connection URL.",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Observation reference timestamp in ISO format (defaults to current UTC).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help="Observation snapshot lookback duration in days (must be 90).",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=10,
        help="Minimum task samples required per evidence cell (default: 10).",
    )
    parser.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=10000,
        help="Number of bootstrap resampling iterations (default: 10000).",
    )
    parser.add_argument(
        "--bootstrap-seed",
        type=int,
        default=29,
        help="Deterministic base random seed for bootstrap resampling (default: 29).",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        type=Path,
        help="Optional path to write deterministic sanitized JSON robustness report.",
    )
    parser.add_argument(
        "--markdown-output",
        default=None,
        type=Path,
        help="Optional path to write deterministic sanitized Markdown robustness report.",
    )
    parser.add_argument(
        "--evidence-scope",
        default="current_execution_cohort",
        choices=["current_execution_cohort", "operational"],
        help="Evidence filtering scope (default: current_execution_cohort).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main CLI execution entrypoint."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)

    db_env_var = args.database_url_env
    db_url = os.getenv(db_env_var)
    if not db_url or not db_url.strip():
        LOGGER.error("Database URL environment variable %r is unset or empty", db_env_var)
        return 1

    try:
        as_of_dt = _parse_as_of(args.as_of)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    if args.lookback_days != 90:
        LOGGER.error("--lookback-days must be exactly 90, got %d", args.lookback_days)
        return 1

    if args.min_samples < 1:
        LOGGER.error("--min-samples must be >= 1, got %d", args.min_samples)
        return 1

    if args.bootstrap_iterations < 1:
        LOGGER.error("--bootstrap-iterations must be >= 1, got %d", args.bootstrap_iterations)
        return 1

    window_start = as_of_dt - timedelta(days=args.lookback_days)
    policy = ProviderReliabilityRobustnessPolicy(
        schema_version=2,
        lookback_days=90,
        min_samples=args.min_samples,
        confidence_level=0.95,
        as_of=as_of_dt,
        window_start_at=window_start,
        window_end_at=as_of_dt,
        windows_days=(30, 60, 90),
        temporal_split_days=45,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
        evidence_scope=args.evidence_scope,
    )

    LOGGER.info(
        "Extracting robustness report snapshot=[%s, %s] min_samples=%d iterations=%d seed=%d",
        window_start.isoformat(),
        as_of_dt.isoformat(),
        args.min_samples,
        args.bootstrap_iterations,
        args.bootstrap_seed,
    )

    try:
        report = extract_provider_reliability_robustness_report(db_url, policy)
    except Exception as exc:
        LOGGER.error("Failed to extract provider reliability robustness report: %s", exc)
        return 1

    json_text = render_json_robustness_report(report)
    md_text = render_markdown_robustness_report(report)

    if args.json_output:
        _write_atomic(args.json_output, json_text)
        LOGGER.info("Wrote JSON robustness report to %s", args.json_output)

    if args.markdown_output:
        _write_atomic(args.markdown_output, md_text)
        LOGGER.info("Wrote Markdown robustness report to %s", args.markdown_output)

    if not args.json_output and not args.markdown_output:
        sys.stdout.write(md_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
