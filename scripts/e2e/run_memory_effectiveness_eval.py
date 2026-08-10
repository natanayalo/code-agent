#!/usr/bin/env python3
"""Run the M28 paired memory-effectiveness baseline and write a JSON report."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.pool import StaticPool

from db.base import Base
from evaluation.memory_effectiveness import (
    evaluate_memory_effectiveness,
    load_memory_effectiveness_suite,
    write_memory_effectiveness_report,
)
from repositories import create_engine_from_url, create_session_factory


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    database_group = parser.add_mutually_exclusive_group()
    database_group.add_argument(
        "--database-url",
        default=None,
        help="Disposable database URL; migrations are applied before evaluation.",
    )
    database_group.add_argument(
        "--postgres-url-env",
        default=None,
        help="Environment variable holding a disposable database URL.",
    )
    parser.add_argument(
        "--suite",
        type=Path,
        default=None,
        help="Path to the M28 memory-effectiveness suite JSON.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluations/m28-memory-effectiveness-report.json"),
        help="Path to write the structured report JSON.",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=20,
        help="Search limit passed to the database-backed load_memory node.",
    )
    return parser


def _apply_migrations(database_url: str) -> None:
    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("script_location", str(Path("db/migrations").resolve()))
    config.set_main_option("sqlalchemy.url", database_url)
    original_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, "head")
    finally:
        if original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_database_url


def _database_url(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str | None:
    if args.database_url:
        return str(args.database_url)
    if args.postgres_url_env:
        value = os.getenv(str(args.postgres_url_env))
        if not value:
            parser.error(f"Environment variable {args.postgres_url_env!r} is not set.")
        return value
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
    return create_session_factory(create_engine_from_url(database_url))


def main() -> int:
    parser = _argument_parser()
    args = parser.parse_args()
    database_url = _database_url(args, parser)
    suite = load_memory_effectiveness_suite(args.suite)
    session_factory = (
        _database_session_factory(database_url)
        if database_url is not None
        else _sqlite_session_factory()
    )
    report = evaluate_memory_effectiveness(
        suite=suite,
        session_factory=session_factory,
        search_limit=args.search_limit,
    )
    write_memory_effectiveness_report(report, args.output)
    print(
        "memory-effectiveness-eval:",
        f"suite={report.suite_name}",
        f"cases={report.total_cases}",
        f"passed={report.passed_cases}",
        f"status={report.status}",
        f"output={args.output}",
    )
    return 0 if report.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
