"""Synthetic integration coverage for M29 read-only provider reliability extractor."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from db.base import Base
from db.enums import (
    ArtifactType,
    OrchestrationRuntime,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import (
    Task,
    TaskTimelineEvent,
    WorkerRun,
)
from evaluation.provider_reliability_extractor import (
    extract_provider_reliability_report,
)
from evaluation.provider_reliability_models import ReliabilityReportPolicy
from evaluation.provider_reliability_report import assert_sanitized_report
from repositories import create_engine_from_url
from scripts.e2e import run_provider_reliability_report as cli
from tests.integration.provider_reliability_support import (
    NOW,
)
from tests.integration.provider_reliability_support import (
    create_test_task as _create_task,
)
from tests.integration.provider_reliability_support import (
    seed_test_database as _seed_test_database,
)


def test_extractor_synthetic_database(tmp_path: Path) -> None:
    """Test full database extraction, stage rates, exclusions, and recommendation."""
    db_url = _seed_test_database(tmp_path)
    policy = ReliabilityReportPolicy(
        schema_version=1,
        lookback_days=90,
        min_samples=10,
        confidence_level=0.95,
        as_of=NOW,
        window_start_at=NOW - timedelta(days=90),
        window_end_at=NOW,
    )

    report = extract_provider_reliability_report(db_url, policy)
    assert report.status == "partial"
    assert report.exclusions.total_tasks_scanned == 28
    assert report.exclusions.included_tasks_count == 20
    assert report.exclusions.excluded_tasks_count == 8

    ex = report.exclusions.by_reason
    assert ex["cancelled"] == 1
    assert ex["incomplete"] == 1
    assert ex["non_temporal_runtime"] == 1
    assert ex["non_native_agent_mode"] == 1
    assert ex["outside_window"] == 1
    assert ex["malformed_missing_task_spec"] == 1
    assert ex["malformed_missing_profile"] == 1
    assert ex["malformed_inconsistent_timeline"] == 1

    assert len(report.evidence_cells) == 4
    cells = {c.profile: c for c in report.evidence_cells}

    codex_cell = cells["codex-native-executor"]
    assert codex_cell.sample_size == 10
    assert codex_cell.accepted_count == 10
    assert codex_cell.accepted_task_rate == 1.0
    assert codex_cell.is_eligible is True
    assert codex_cell.manual_overrides_count == 1
    assert codex_cell.repairs.verifier_repairs_count == 1
    assert codex_cell.interventions.clarification_questions_count == 1
    assert codex_cell.stage_outcome_rates.verification_pass_rate is None
    assert codex_cell.stage_outcome_rates.review_pass_rate is None

    ag_cell = cells["antigravity-native-executor"]
    assert ag_cell.sample_size == 10
    assert ag_cell.accepted_count == 8
    assert ag_cell.failure_count == 2
    assert ag_cell.typed_failures == {"compile_error": 2}
    assert ag_cell.is_eligible is True

    ro_codex = cells["codex-native-executor-read-only"]
    assert ro_codex.sample_size == 0
    assert ro_codex.is_eligible is False

    assert len(report.profile_coverage) == 4
    cov = {c.profile: c for c in report.profile_coverage}
    assert cov["codex-native-executor"].is_eligible is True
    assert cov["codex-native-executor-read-only"].has_evidence is False

    assert len(report.recommendations) == 2
    recs = {(r.task_class, r.mutation_mode): r for r in report.recommendations}
    feat_rec = recs[("feature", "mutation")]
    assert feat_rec.recommended_profile == "codex-native-executor"
    assert feat_rec.fallback_reason is None
    assert len(feat_rec.rankings) == 2
    assert feat_rec.rankings[0].profile == "codex-native-executor"
    assert feat_rec.rankings[0].rank == 1
    assert feat_rec.rankings[1].profile == "antigravity-native-executor"
    assert feat_rec.rankings[1].rank == 2

    scout_rec = recs[("scout", "read_only")]
    assert scout_rec.recommended_profile is None
    assert scout_rec.fallback_reason is not None


def test_cli_execution_and_clean_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test CLI runs cleanly, respects flags, and writes deterministic sanitized reports."""
    db_url = _seed_test_database(tmp_path)
    env_var = "TEST_CODE_AGENT_DATABASE_URL"
    monkeypatch.setenv(env_var, db_url)

    json_out = tmp_path / "out" / "report.json"
    md_out = tmp_path / "out" / "report.md"

    exit_code = cli.main(
        [
            "--database-url-env",
            env_var,
            "--as-of",
            NOW.isoformat(),
            "--lookback-days",
            "90",
            "--min-samples",
            "10",
            "--json-output",
            str(json_out),
            "--markdown-output",
            str(md_out),
        ]
    )
    assert exit_code == 0
    assert json_out.exists()
    assert md_out.exists()

    json_text = json_out.read_text(encoding="utf-8")
    assert_sanitized_report(json_text)
    data = json.loads(json_text)
    assert data["schema_version"] == 1
    assert data["status"] == "partial"
    assert len(data["profile_coverage"]) == 4

    md_text = md_out.read_text(encoding="utf-8")
    assert "M29 Provider Reliability Advisory Report" in md_text
    assert "Enabled Profile Catalog Coverage" in md_text
    assert "codex-native-executor" in md_text
    assert "antigravity-native-executor" in md_text
    for forbidden in ["task_id", "task_text", "repo_url", "branch", "secret", "private text"]:
        assert forbidden not in md_text


def test_cli_failure_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test CLI error handling on missing env var and invalid parameters."""
    assert cli.main(["--database-url-env", "NONEXISTENT_VAR"]) == 1

    monkeypatch.setenv("TEST_EMPTY_URL", "sqlite:///:memory:")
    assert cli.main(["--database-url-env", "TEST_EMPTY_URL", "--as-of", "not-a-date"]) == 1
    assert cli.main(["--database-url-env", "TEST_EMPTY_URL", "--lookback-days", "0"]) == 1
    assert cli.main(["--database-url-env", "TEST_EMPTY_URL", "--min-samples", "0"]) == 1


def test_verification_passes_delivery_fails(tmp_path: Path) -> None:
    """Ensure verification pass is preserved when delivery subsequently fails."""
    db_path = tmp_path / "test_stage_outcomes.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    task = _create_task(
        task_id="v-pass-d-fail",
        status=TaskStatus.FAILED,
        verification_cmds=["pytest"],
        delivery_mode="branch",
    )
    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=len(task.timeline_events),
            event_type=TimelineEventType.VERIFICATION_COMPLETED,
            created_at=task.created_at + timedelta(minutes=2),
            payload={"status": "passed"},
        )
    )
    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=len(task.timeline_events),
            event_type=TimelineEventType.DELIVERY_FAILED,
            created_at=task.created_at + timedelta(minutes=4),
            payload={"error": "push rejected"},
        )
    )
    task.worker_runs[0].status = WorkerRunStatus.SUCCESS

    with Session(engine) as session:
        session.add(task)
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)
    cell = [c for c in report.evidence_cells if c.profile == "codex-native-executor"][0]
    assert cell.sample_size == 1
    assert cell.accepted_count == 0
    assert cell.stage_outcome_rates.verification_pass_rate == 1.0
    assert cell.stage_outcome_rates.delivery_pass_rate == 0.0


def test_review_stage_outcome_and_applicability(tmp_path: Path) -> None:
    """Ensure review pass requires review artifact and parses ReviewResult outcome."""
    db_path = tmp_path / "test_review_applicability.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    task_no_review = _create_task(
        task_id="no-review",
        status=TaskStatus.COMPLETED,
        verification_cmds=["pytest"],
    )
    task_pass = _create_task(
        task_id="review-pass",
        status=TaskStatus.COMPLETED,
        verification_cmds=["pytest"],
    )
    task_pass.worker_runs[0].artifact_index = [
        {
            "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
            "artifact_metadata": {
                ArtifactType.INDEPENDENT_REVIEW_RESULT.value: {
                    "outcome": "no_findings",
                    "findings": [],
                }
            },
        }
    ]
    task_fail = _create_task(
        task_id="review-fail",
        status=TaskStatus.COMPLETED,
        verification_cmds=["pytest"],
    )
    task_fail.worker_runs[0].artifact_index = [
        {
            "artifact_type": ArtifactType.INDEPENDENT_REVIEW_RESULT.value,
            "artifact_metadata": {
                ArtifactType.INDEPENDENT_REVIEW_RESULT.value: {
                    "outcome": "findings",
                    "findings": [{"message": "bug"}],
                }
            },
        }
    ]
    with Session(engine) as session:
        session.add_all([task_no_review, task_pass, task_fail])
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)
    cell = [c for c in report.evidence_cells if c.profile == "codex-native-executor"][0]
    assert cell.sample_size == 3
    assert cell.stage_outcome_rates.review_pass_rate == 0.5


def test_branch_task_lacking_delivery_completed_not_delivery_pass(tmp_path: Path) -> None:
    """Ensure branch task without DELIVERY_COMPLETED is not reported as delivery pass."""
    db_path = tmp_path / "test_delivery_missing.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    task = _create_task(
        task_id="no-deliv-event",
        status=TaskStatus.COMPLETED,
        delivery_mode="branch",
    )
    task.worker_runs[0].delivery_metadata = {}
    with Session(engine) as session:
        session.add(task)
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)
    cell = [c for c in report.evidence_cells if c.profile == "codex-native-executor"][0]
    assert cell.stage_outcome_rates.delivery_pass_rate == 0.0


def test_mixed_profile_execution_excluded(tmp_path: Path) -> None:
    """Ensure multi-profile retries are excluded under mixed_profile_execution."""
    db_path = tmp_path / "test_mixed_profile.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    task = _create_task(
        task_id="mixed-task",
        status=TaskStatus.COMPLETED,
        profile="antigravity-native-executor",
    )
    task.worker_runs.insert(
        0,
        WorkerRun(
            worker_type=WorkerType.CODEX,
            worker_profile="codex-native-executor",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            orchestration_runtime=OrchestrationRuntime.TEMPORAL,
            started_at=task.created_at - timedelta(minutes=20),
            finished_at=task.created_at - timedelta(minutes=15),
            status=WorkerRunStatus.FAILURE,
            verifier_outcome={"status": "failed", "failure_kind": "test_failure"},
            budget_usage={"tokens": 50},
        ),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)
    assert report.exclusions.by_reason.get("mixed_profile_execution") == 1
    assert report.exclusions.included_tasks_count == 0


def test_zero_sample_enabled_profile_appears_in_cells_and_rankings(tmp_path: Path) -> None:
    """Ensure enabled profiles with zero tasks appear with is_eligible=False and 0 samples."""
    db_path = tmp_path / "test_zero_sample.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        for idx in range(10):
            session.add(
                _create_task(
                    task_id=f"codex-{idx}",
                    status=TaskStatus.COMPLETED,
                    profile="codex-native-executor",
                )
            )
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=10,
    )
    report = extract_provider_reliability_report(db_url, policy)
    cells_by_profile = {c.profile: c for c in report.evidence_cells}
    assert "antigravity-native-executor" in cells_by_profile
    ag_cell = cells_by_profile["antigravity-native-executor"]
    assert ag_cell.sample_size == 0
    assert ag_cell.is_eligible is False
    assert any("insufficient_sample_size: 0 tasks" in r for r in ag_cell.insufficiency_reasons)


def test_manual_profile_override_counted(tmp_path: Path) -> None:
    """Ensure manual_profile_override route reason is counted in overrides."""
    db_path = tmp_path / "test_override.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    task = _create_task(
        task_id="profile-override-task",
        status=TaskStatus.COMPLETED,
    )
    task.route_reason = "manual_profile_override"
    with Session(engine) as session:
        session.add(task)
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)
    cell = [c for c in report.evidence_cells if c.profile == "codex-native-executor"][0]
    assert cell.manual_overrides_count == 1
    assert cell.manual_override_rate == 1.0


def test_unordered_multiple_worker_runs_deterministic_failure(tmp_path: Path) -> None:
    """Ensure worker runs are sorted by (started_at, id) for deterministic failure resolution."""
    db_path = tmp_path / "test_sort_runs.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    task = _create_task(task_id="multi-run-task", status=TaskStatus.FAILED)
    task.worker_runs.clear()

    def _make_run(run_id: str, min_offset: int, kind: str) -> WorkerRun:
        return WorkerRun(
            id=run_id,
            worker_type=WorkerType.CODEX,
            worker_profile="codex-native-executor",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            orchestration_runtime=OrchestrationRuntime.TEMPORAL,
            started_at=task.created_at + timedelta(minutes=min_offset),
            finished_at=task.created_at + timedelta(minutes=min_offset + 2),
            status=WorkerRunStatus.FAILURE,
            verifier_outcome={"status": "failed", "failure_kind": kind},
        )

    task.worker_runs.extend(
        [_make_run("run-b", 5, "latest_failure"), _make_run("run-a", 1, "earlier_failure")]
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)
    cell = [c for c in report.evidence_cells if c.profile == "codex-native-executor"][0]
    assert "latest_failure" in cell.typed_failures
    assert "earlier_failure" not in cell.typed_failures


def test_failed_task_with_cancelled_event_rejected(tmp_path: Path) -> None:
    """Ensure task with FAILED status and TASK_CANCELLED event is rejected."""
    db_path = tmp_path / "test_cancelled_timeline.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    task = _create_task(
        task_id="failed-but-cancelled",
        status=TaskStatus.FAILED,
    )
    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=len(task.timeline_events),
            event_type=TimelineEventType.TASK_CANCELLED,
            created_at=task.created_at + timedelta(minutes=4),
        )
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)
    assert report.exclusions.by_reason.get("malformed_inconsistent_timeline") == 1
    assert report.exclusions.included_tasks_count == 0


def test_extractor_all_profiles_complete(tmp_path: Path) -> None:
    """Ensure report achieves complete status when all profile groups have sufficient evidence."""
    db_path = tmp_path / "test_all_complete.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    tasks: list[Task] = []
    groups = [
        ("feat-codex", "feature", "codex-native-executor"),
        ("feat-ag", "feature", "antigravity-native-executor"),
        ("scout-codex", "scout", "codex-native-executor-read-only"),
        ("scout-ag", "scout", "antigravity-native-executor-read-only"),
    ]
    for prefix, t_class, prof in groups:
        for idx in range(10):
            tasks.append(
                _create_task(
                    task_id=f"{prefix}-{idx}",
                    status=TaskStatus.COMPLETED,
                    task_class=t_class,
                    profile=prof,
                )
            )

    with Session(engine) as session:
        session.add_all(tasks)
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=10,
    )
    report = extract_provider_reliability_report(db_url, policy)
    assert report.status == "complete"
    assert len(report.profile_coverage) == 4
    assert all(cov.is_eligible for cov in report.profile_coverage)
    assert len(report.recommendations) == 2
    assert all(r.fallback_reason is None for r in report.recommendations)
    assert all(r.recommended_profile is not None for r in report.recommendations)


def test_task_boundary_prefilter_and_window_handling(tmp_path: Path) -> None:
    """Verify SQL boundary counts for older/newer tasks and inclusion of long-running tasks."""
    db_path = tmp_path / "test_boundary.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    # 1. Created before window, completed inside window -> included
    long_task = _create_task(
        task_id="long-task",
        status=TaskStatus.COMPLETED,
        created_at=NOW - timedelta(days=60),
        updated_at=NOW - timedelta(days=5),
    )
    # 2. Older task: updated before window start -> counted via SQL older_count
    old_task = _create_task(
        task_id="old-task",
        status=TaskStatus.COMPLETED,
        created_at=NOW - timedelta(days=120),
        updated_at=NOW - timedelta(days=100),
    )
    # 3. Newer task: created after window end -> counted via SQL newer_count
    future_task = _create_task(
        task_id="future-task",
        status=TaskStatus.COMPLETED,
        created_at=NOW + timedelta(days=10),
        updated_at=NOW + timedelta(days=11),
    )
    with Session(engine) as session:
        session.add_all([long_task, old_task, future_task])
        session.commit()

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW,
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)
    assert report.exclusions.total_tasks_scanned == 3
    assert report.exclusions.included_tasks_count == 1
    assert report.exclusions.by_reason.get("outside_window") == 2
    cell = [c for c in report.evidence_cells if c.profile == "codex-native-executor"][0]
    assert cell.sample_size == 1
    assert cell.terminal_latency.median_seconds == 4752000.0
