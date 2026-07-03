#!/usr/bin/env python3
"""Run deterministic memory-retrieval evaluation and write a JSON report."""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy.pool import StaticPool

from db.base import Base
from evaluation import (
    evaluate_memory_retrieval,
    load_memory_retrieval_suite,
    write_memory_retrieval_report,
)
from repositories import create_engine_from_url, create_session_factory


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        type=Path,
        default=None,
        help="Path to memory retrieval suite JSON.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluations/memory-retrieval-report.json"),
        help="Path to write the structured report JSON.",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=20,
        help="Search limit passed to the load_memory node.",
    )
    parser.add_argument(
        "--fail-under-recall",
        type=float,
        default=None,
        help="Optional minimum non-semantic-gap recall threshold.",
    )
    return parser


def _sqlite_session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def main() -> int:
    args = _build_argument_parser().parse_args()
    suite = load_memory_retrieval_suite(path=args.suite)
    report = evaluate_memory_retrieval(
        suite=suite,
        session_factory=_sqlite_session_factory(),
        search_limit=args.search_limit,
    )
    write_memory_retrieval_report(report, args.output)
    recall_text = "n/a" if report.recall is None else f"{report.recall:.3f}"
    print(
        "memory-retrieval-eval:",
        f"suite={report.suite_name}",
        f"cases={report.total_cases}",
        f"recall={recall_text}",
        f"regression_misses={len(report.regression_misses)}",
        f"known_semantic_gap_misses={len(report.known_semantic_gap_misses)}",
        f"output={args.output}",
    )
    if args.fail_under_recall is not None:
        if report.recall is not None and report.recall < args.fail_under_recall:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
