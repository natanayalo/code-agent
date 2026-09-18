"""Integration tests for M29 provider reliability robustness analysis with synthetic SQLite."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from db.base import Base
from db.enums import TaskStatus
from db.models import Task
from evaluation.provider_reliability_models import ProviderReliabilityRobustnessPolicy
from evaluation.provider_reliability_robustness import (
    extract_provider_reliability_robustness_report,
)
from evaluation.provider_reliability_robustness_report import (
    assert_sanitized_robustness_report,
    render_json_robustness_report,
    render_markdown_robustness_report,
)
from repositories import create_engine_from_url
from scripts.e2e import run_provider_reliability_robustness as cli
from tests.integration.provider_reliability_support import (
    NOW,
    create_test_task,
)


def _build_recent_cohort_tasks() -> list[Task]:
    """Build recent cohort synthetic tasks across 30d and 60d windows."""
    tasks: list[Task] = []
    # Recent cohort: days 1 to 20 (within 30d window: 7 tasks each)
    for i in range(7):
        ts = NOW - timedelta(days=i * 3 + 1)
        tasks.append(
            create_test_task(
                task_id=f"rec-30-codex-{i}",
                status=TaskStatus.COMPLETED,
                task_class="feature",
                profile="codex-native-executor",
                created_at=ts - timedelta(minutes=5),
                updated_at=ts,
            )
        )
        tasks.append(
            create_test_task(
                task_id=f"rec-30-ag-{i}",
                status=TaskStatus.COMPLETED,
                task_class="feature",
                profile="antigravity-native-executor",
                created_at=ts - timedelta(minutes=5),
                updated_at=ts,
            )
        )

    # Recent cohort: days 32 to 44
    # (within 60d window & recent cohort: 3 more tasks each -> 10 total)
    for i in range(3):
        ts = NOW - timedelta(days=32 + i * 3)
        tasks.append(
            create_test_task(
                task_id=f"rec-60-codex-{i}",
                status=TaskStatus.COMPLETED,
                task_class="feature",
                profile="codex-native-executor",
                created_at=ts - timedelta(minutes=5),
                updated_at=ts,
            )
        )
        tasks.append(
            create_test_task(
                task_id=f"rec-60-ag-{i}",
                status=TaskStatus.COMPLETED,
                task_class="feature",
                profile="antigravity-native-executor",
                created_at=ts - timedelta(minutes=5),
                updated_at=ts,
            )
        )
    return tasks


def _build_historical_and_excluded_tasks() -> list[Task]:
    """Build historical cohort and excluded synthetic tasks."""
    tasks: list[Task] = []
    # Historical cohort: days 50 to 80 (between 45d and 90d: 5 tasks each)
    for i in range(5):
        ts = NOW - timedelta(days=50 + i * 6)
        tasks.append(
            create_test_task(
                task_id=f"hist-codex-{i}",
                status=TaskStatus.COMPLETED,
                task_class="feature",
                profile="codex-native-executor",
                created_at=ts - timedelta(minutes=5),
                updated_at=ts,
            )
        )
        tasks.append(
            create_test_task(
                task_id=f"hist-ag-{i}",
                status=TaskStatus.COMPLETED,
                task_class="feature",
                profile="antigravity-native-executor",
                created_at=ts - timedelta(minutes=5),
                updated_at=ts,
            )
        )

    # Excluded tasks
    tasks.append(create_test_task(task_id="cancelled-task", status=TaskStatus.CANCELLED))
    tasks.append(
        create_test_task(
            task_id="outside-window-task",
            status=TaskStatus.COMPLETED,
            created_at=NOW - timedelta(days=120),
            updated_at=NOW - timedelta(days=119),
        )
    )
    return tasks


def _seed_robustness_tasks(tmp_path: Path) -> str:
    """Seed synthetic tasks distributed across lookback windows and cohorts."""
    db_path = tmp_path / "robustness_integration.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    tasks = _build_recent_cohort_tasks() + _build_historical_and_excluded_tasks()

    with Session(engine) as session:
        for t in tasks:
            session.add(t)
        session.commit()
    engine.dispose()
    return db_url


def test_robustness_extractor_synthetic_integration(tmp_path: Path) -> None:
    """Validate full robustness analysis across 30/60/90 windows, cohorts, and bootstrap."""
    db_url = _seed_robustness_tasks(tmp_path)
    policy = ProviderReliabilityRobustnessPolicy(
        schema_version=2,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=NOW,
        window_start_at=NOW - timedelta(days=90),
        window_end_at=NOW,
        windows_days=(30, 60, 90),
        temporal_split_days=45,
        bootstrap_iterations=200,
        bootstrap_seed=29,
    )

    report = extract_provider_reliability_robustness_report(db_url, policy)
    assert report.status == "partial"  # feature is recommended; scout is not

    # Verify exclusions describe full 90-day snapshot
    assert report.exclusions.total_tasks_scanned == 32
    assert report.exclusions.included_tasks_count == 30
    assert report.exclusions.excluded_tasks_count == 2
    assert "cancelled" in report.exclusions.by_reason
    assert "outside_window" in report.exclusions.by_reason

    # Verify windows
    assert len(report.windows) == 3
    w_by_days = {w.window_days: w for w in report.windows}

    # 30d window: 7 tasks per profile (total 14) -> below floor of 10 -> fallback
    w30 = w_by_days[30]
    assert w30.included_tasks_count == 14
    w30_rec = next(r for r in w30.recommendations if r.task_class == "feature")
    assert w30_rec.recommended_profile is None
    assert "lack sufficient samples" in (w30_rec.fallback_reason or "")

    # 60d window: 12 tasks per profile (total 24) -> meets floor of 10 -> recommended!
    w60 = w_by_days[60]
    assert w60.included_tasks_count == 24
    w60_rec = next(r for r in w60.recommendations if r.task_class == "feature")
    assert w60_rec.recommended_profile is not None

    # 90d window: 15 tasks per profile (total 30) -> meets floor -> recommended!
    w90 = w_by_days[90]
    assert w90.included_tasks_count == 30
    w90_rec = next(r for r in w90.recommendations if r.task_class == "feature")
    assert w90_rec.recommended_profile is not None

    # Verify cohorts
    c_by_name = {c.cohort_name: c for c in report.temporal_cohorts}
    hist = c_by_name["historical"]
    assert hist.included_tasks_count == 10  # 5 per profile
    hist_rec = next(r for r in hist.recommendations if r.task_class == "feature")
    assert hist_rec.recommended_profile is None  # 5 < 10 floor
    assert "lack sufficient samples" in (hist_rec.fallback_reason or "")

    rec = c_by_name["recent"]
    assert rec.included_tasks_count == 20  # 10 per profile
    rec_rec = next(r for r in rec.recommendations if r.task_class == "feature")
    assert rec_rec.recommended_profile is not None

    # Verify bootstrap
    b_feat = next(b for b in report.bootstrap_results if b.task_class == "feature")
    assert b_feat.status == "complete"
    assert len(b_feat.candidates) == 2
    assert b_feat.eligible_profiles == [
        "antigravity-native-executor",
        "codex-native-executor",
    ]

    # Verify rendered outputs conform to public allowlists
    json_out = render_json_robustness_report(report)
    assert_sanitized_robustness_report(json_out)
    md_out = render_markdown_robustness_report(report)
    assert "# M29 Provider Reliability Robustness Report" in md_out


def test_robustness_cli_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test operator CLI execution with env var, atomic file writes, and clean outputs."""
    db_url = _seed_robustness_tasks(tmp_path)
    monkeypatch.setenv("TEST_ROBUSTNESS_DB_URL", db_url)

    json_path = tmp_path / "robustness_report.json"
    md_path = tmp_path / "robustness_report.md"

    exit_code = cli.main(
        [
            "--database-url-env",
            "TEST_ROBUSTNESS_DB_URL",
            "--as-of",
            NOW.isoformat(),
            "--lookback-days",
            "90",
            "--min-samples",
            "10",
            "--bootstrap-iterations",
            "100",
            "--bootstrap-seed",
            "29",
            "--json-output",
            str(json_path),
            "--markdown-output",
            str(md_path),
        ]
    )
    assert exit_code == 0
    assert json_path.is_file()
    assert md_path.is_file()

    json_content = json_path.read_text(encoding="utf-8")
    assert_sanitized_robustness_report(json_content)
    parsed = json.loads(json_content)
    assert parsed["schema_version"] == 2
    assert parsed["policy"]["lookback_days"] == 90
    assert parsed["policy"]["bootstrap_iterations"] == 100


def test_robustness_cli_invalid_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate CLI failure paths for invalid lookback_days, missing env, or future as_of."""
    monkeypatch.setenv("TEST_DB_URL", "sqlite:///:memory:")

    # Non-90 lookback_days
    assert cli.main(["--database-url-env", "TEST_DB_URL", "--lookback-days", "60"]) == 1

    # Missing database environment variable
    assert cli.main(["--database-url-env", "UNSET_ROBUSTNESS_DB_ENV_VAR"]) == 1

    # Materially future as-of timestamp
    future_ts = (NOW + timedelta(days=5)).isoformat()
    assert cli.main(["--database-url-env", "TEST_DB_URL", "--as-of", future_ts]) == 1
