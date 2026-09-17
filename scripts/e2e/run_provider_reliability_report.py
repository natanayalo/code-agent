#!/usr/bin/env python3
"""Operator CLI to generate the M29 Provider Reliability Advisory Report."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evaluation.provider_reliability_extractor import (
    extract_provider_reliability_report,
)
from evaluation.provider_reliability_models import ReliabilityReportPolicy
from evaluation.provider_reliability_report import (
    render_json_report,
    render_markdown_report,
)

LOGGER = logging.getLogger("run_provider_reliability_report")


def _write_atomic(path: Path, content: str) -> None:
    """Write text atomically to a path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(f"{path.suffix}.pending")
    pending.write_text(content, encoding="utf-8")
    pending.replace(path)


def _parse_as_of(val: str | None) -> datetime:
    """Parse as-of timestamp into UTC datetime."""
    if not val:
        return datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO timestamp for --as-of: {val}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI arguments parser."""
    parser = argparse.ArgumentParser(
        description="Generate offline M29 provider reliability advisory report."
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
        help="Evidence window duration in days (default: 90).",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=10,
        help="Minimum task samples required per evidence cell (default: 10).",
    )
    parser.add_argument(
        "--json-output",
        default=None,
        type=Path,
        help="Optional path to write deterministic sanitized JSON report.",
    )
    parser.add_argument(
        "--markdown-output",
        default=None,
        type=Path,
        help="Optional path to write deterministic sanitized Markdown report.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main CLI execution loop."""
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

    if args.lookback_days < 1:
        LOGGER.error("--lookback-days must be >= 1, got %d", args.lookback_days)
        return 1

    if args.min_samples < 1:
        LOGGER.error("--min-samples must be >= 1, got %d", args.min_samples)
        return 1

    window_start = as_of_dt - timedelta(days=args.lookback_days)
    policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=args.lookback_days,
        min_samples=args.min_samples,
        confidence_level=0.95,
        as_of=as_of_dt,
        window_start_at=window_start,
        window_end_at=as_of_dt,
    )

    LOGGER.info(
        "Extracting provider reliability report window=[%s, %s] min_samples=%d",
        window_start.isoformat(),
        as_of_dt.isoformat(),
        args.min_samples,
    )

    try:
        report = extract_provider_reliability_report(db_url, policy)
    except Exception as exc:
        LOGGER.error("Failed to extract provider reliability report: %s", exc)
        return 1

    json_text = render_json_report(report)
    md_text = render_markdown_report(report)

    if args.json_output:
        _write_atomic(args.json_output, json_text)
        LOGGER.info("Wrote JSON report to %s", args.json_output)

    if args.markdown_output:
        _write_atomic(args.markdown_output, md_text)
        LOGGER.info("Wrote Markdown report to %s", args.markdown_output)

    if not args.json_output and not args.markdown_output:
        sys.stdout.write(md_text)

    LOGGER.info(
        "Provider reliability report generated: status=%s cells=%d recommendations=%d",
        report.status,
        len(report.evidence_cells),
        len(report.recommendations),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
