#!/usr/bin/env python3
"""Run deterministic memory-extraction evaluation and write a JSON report."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.pool import StaticPool

from db.base import Base
from evaluation import (
    evaluate_memory_extraction,
    load_memory_extraction_suite,
    write_memory_extraction_report,
)
from repositories import create_engine_from_url, create_session_factory


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    database_group = parser.add_mutually_exclusive_group()
    database_group.add_argument(
        "--database-url",
        default=None,
        help=(
            "Disposable test database URL to evaluate against. "
            "The runner applies Alembic migrations and seeds evaluation observations."
        ),
    )
    database_group.add_argument(
        "--postgres-url-env",
        default=None,
        help=(
            "Environment variable containing a disposable Postgres test database URL. "
            "The runner applies Alembic migrations and seeds evaluation observations."
        ),
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path("evaluation/memory_extraction_suite.json"),
        help="Path to memory extraction suite JSON.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluations/memory-extraction-report.json"),
        help="Path to write the structured report JSON.",
    )
    parser.add_argument(
        "--fail-under-precision",
        type=float,
        default=None,
        help="Optional minimum extraction precision threshold.",
    )
    parser.add_argument(
        "--fail-under-recall",
        type=float,
        default=None,
        help="Optional minimum extraction recall threshold.",
    )
    return parser


def _apply_migrations(database_url: str) -> None:
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _database_url_from_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> str | None:
    if args.database_url:
        return str(args.database_url)
    if args.postgres_url_env:
        database_url = os.getenv(str(args.postgres_url_env))
        if not database_url:
            parser.error(f"Environment variable {args.postgres_url_env!r} is not set.")
        return database_url
    return None


def _sqlite_session_factory():
    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _database_session_factory(database_url: str):
    _apply_migrations(database_url)
    engine = create_engine_from_url(database_url)
    return create_session_factory(engine)


def main() -> int:
    parser = _build_argument_parser()
    args = parser.parse_args()
    database_url = _database_url_from_args(args, parser)
    suite = load_memory_extraction_suite(path=args.suite)
    session_factory = (
        _database_session_factory(database_url)
        if database_url is not None
        else _sqlite_session_factory()
    )
    report = evaluate_memory_extraction(
        suite=suite,
        session_factory=session_factory,
    )
    write_memory_extraction_report(report, args.output)

    prec_text = "n/a" if report.precision is None else f"{report.precision:.3f}"
    rec_text = "n/a" if report.recall is None else f"{report.recall:.3f}"
    print(
        "memory-extraction-eval:",
        f"suite={report.suite_name}",
        f"cases={report.total_cases}",
        f"passed_cases={report.passed_cases}",
        f"failed_cases={report.failed_cases}",
        f"precision={prec_text}",
        f"recall={rec_text}",
        f"output={args.output}",
    )
    print("Quality metrics:")
    for k, v in report.quality_metrics.items():
        print(f"  {k}: {v}")

    # Check thresholds
    if args.fail_under_precision is not None:
        if report.precision is not None and report.precision < args.fail_under_precision:
            print(
                f"Failed: precision {report.precision:.3f} "
                f"is below threshold {args.fail_under_precision:.3f}"
            )
            return 1
    if args.fail_under_recall is not None:
        if report.recall is not None and report.recall < args.fail_under_recall:
            print(
                f"Failed: recall {report.recall:.3f} "
                f"is below threshold {args.fail_under_recall:.3f}"
            )
            return 1

    if report.failed_cases > 0:
        print("Failed cases detected.")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
