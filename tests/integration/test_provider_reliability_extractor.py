"""Synthetic integration coverage for M29 read-only provider reliability extractor."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from db.base import Base
from db.enums import (
    HumanInteractionType,
    OrchestrationRuntime,
    TaskStatus,
    TimelineEventType,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from db.models import (
    HumanInteraction,
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

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _add_worker_run(
    task: Task,
    profile: str,
    mode: WorkerRuntimeMode,
    runtime: OrchestrationRuntime,
    status: TaskStatus,
    created_at: datetime,
    updated_at: datetime,
    failure_kind: str | None,
    budget: dict | None,
) -> None:
    """Add a worker run attempt to the task."""
    run_status = (
        WorkerRunStatus.SUCCESS if status == TaskStatus.COMPLETED else WorkerRunStatus.FAILURE
    )
    if status == TaskStatus.COMPLETED:
        verifier_outcome = {"status": "passed"}
    else:
        verifier_outcome = {"status": "failed", "failure_kind": failure_kind or "test_failure"}
    task.worker_runs.append(
        WorkerRun(
            worker_type=WorkerType.CODEX,
            worker_profile=profile,
            runtime_mode=mode,
            orchestration_runtime=runtime,
            started_at=created_at,
            finished_at=updated_at,
            status=run_status,
            verifier_outcome=verifier_outcome,
            budget_usage=budget or {"tokens": 100},
        )
    )


def _add_timeline_events(
    task: Task,
    status: TaskStatus,
    created_at: datetime,
    updated_at: datetime,
    inconsistent: bool,
) -> None:
    """Attach ingested and terminal timeline events."""
    if inconsistent:
        terminal_type = TimelineEventType.TASK_FAILED
    elif status == TaskStatus.COMPLETED:
        terminal_type = TimelineEventType.TASK_COMPLETED
    elif status == TaskStatus.FAILED:
        terminal_type = TimelineEventType.TASK_FAILED
    elif status == TaskStatus.CANCELLED:
        terminal_type = TimelineEventType.TASK_CANCELLED
    else:
        terminal_type = TimelineEventType.WORKER_DISPATCHED

    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=0,
            event_type=TimelineEventType.TASK_INGESTED,
            created_at=created_at,
        )
    )
    task.timeline_events.append(
        TaskTimelineEvent(
            attempt_number=0,
            sequence_number=1,
            event_type=terminal_type,
            created_at=updated_at,
        )
    )


def _add_clarifications(task: Task, count: int, created_at: datetime) -> None:
    """Attach human clarification interactions."""
    for q_idx in range(count):
        task.human_interactions.append(
            HumanInteraction(
                interaction_type=HumanInteractionType.CLARIFICATION,
                summary=f"private clarification question {q_idx}",
                data={"questions": ["Clarify?"]},
                created_at=created_at,
            )
        )


def _create_task(
    *,
    task_id: str,
    status: TaskStatus,
    task_class: str = "feature",
    profile: str = "codex-native-executor",
    runtime: OrchestrationRuntime = OrchestrationRuntime.TEMPORAL,
    mode: WorkerRuntimeMode = WorkerRuntimeMode.NATIVE_AGENT,
    created_at: datetime = NOW - timedelta(days=10),
    updated_at: datetime = NOW - timedelta(days=10, minutes=-5),
    with_spec: bool = True,
    with_profile: bool = True,
    inconsistent_timeline: bool = False,
    verification_cmds: list[str] | None = None,
    delivery_mode: str = "workspace",
    has_override: bool = False,
    failure_kind: str | None = None,
    budget: dict | None = None,
    verifier_repair: bool = False,
    clarifications: int = 0,
) -> Task:
    """Helper to construct a synthetic task with complete graph state."""
    spec = None
    if with_spec:
        spec = {
            "task_type": task_class,
            "allowed_actions": ["modify_workspace_files"],
            "verification_commands": verification_cmds or [],
            "delivery_mode": delivery_mode,
        }

    constraints: dict = {}
    if verifier_repair:
        constraints["independent_verifier_repair_passes_used"] = 1
    if has_override:
        constraints["worker_override"] = "codex"

    task = Task(
        session_id="synthetic-session-id",
        task_text=f"private text for {task_id}",
        repo_url="https://github.invalid/private/repo",
        branch="feature-private",
        status=status,
        chosen_worker=WorkerType.CODEX,
        chosen_profile=profile if with_profile else None,
        runtime_mode=mode,
        orchestration_runtime=runtime,
        task_spec=spec,
        constraints=constraints,
        worker_override=WorkerType.CODEX if has_override else None,
        created_at=created_at,
        updated_at=updated_at,
    )

    _add_worker_run(
        task,
        profile if with_profile else None,
        mode,
        runtime,
        status,
        created_at,
        updated_at,
        failure_kind,
        budget,
    )
    _add_timeline_events(task, status, created_at, updated_at, inconsistent_timeline)
    _add_clarifications(task, clarifications, created_at)
    return task


def _seed_valid_tasks() -> list[Task]:
    """Generate 20 valid tasks for codex and antigravity."""
    tasks: list[Task] = []
    for idx in range(10):
        tasks.append(
            _create_task(
                task_id=f"codex-success-{idx}",
                status=TaskStatus.COMPLETED,
                task_class="feature",
                profile="codex-native-executor",
                has_override=(idx == 0),
                verifier_repair=(idx == 1),
                clarifications=(1 if idx == 2 else 0),
            )
        )
    for idx in range(8):
        tasks.append(
            _create_task(
                task_id=f"antigravity-success-{idx}",
                status=TaskStatus.COMPLETED,
                task_class="feature",
                profile="antigravity-native-executor",
            )
        )
    for idx in range(2):
        tasks.append(
            _create_task(
                task_id=f"antigravity-failed-{idx}",
                status=TaskStatus.FAILED,
                task_class="feature",
                profile="antigravity-native-executor",
                failure_kind="compile_error",
            )
        )
    return tasks


def _seed_excluded_tasks() -> list[Task]:
    """Generate excluded task variants covering all exclusion reasons."""
    return [
        _create_task(task_id="cancelled-1", status=TaskStatus.CANCELLED),
        _create_task(task_id="incomplete-1", status=TaskStatus.IN_PROGRESS),
        _create_task(
            task_id="legacy-runtime",
            status=TaskStatus.COMPLETED,
            runtime=OrchestrationRuntime.LEGACY,
        ),
        _create_task(
            task_id="tool-loop-mode",
            status=TaskStatus.COMPLETED,
            mode=WorkerRuntimeMode.TOOL_LOOP,
        ),
        _create_task(
            task_id="old-task",
            status=TaskStatus.COMPLETED,
            created_at=NOW - timedelta(days=120),
            updated_at=NOW - timedelta(days=119),
        ),
        _create_task(task_id="missing-spec", status=TaskStatus.COMPLETED, with_spec=False),
        _create_task(task_id="missing-profile", status=TaskStatus.COMPLETED, with_profile=False),
        _create_task(
            task_id="inconsistent-timeline",
            status=TaskStatus.COMPLETED,
            inconsistent_timeline=True,
        ),
    ]


def _seed_test_database(tmp_path: Path) -> str:
    """Seed synthetic tasks covering successes, failures, exclusions, and edge cases."""
    db_path = tmp_path / "synthetic_m29.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    all_tasks = _seed_valid_tasks() + _seed_excluded_tasks()

    with Session(engine) as session:
        for t in all_tasks:
            session.add(t)
        session.commit()
    engine.dispose()
    return db_url


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
    assert report.status == "complete"
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

    assert len(report.evidence_cells) == 2
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

    assert len(report.recommendations) == 1
    rec = report.recommendations[0]
    assert rec.task_class == "feature"
    assert rec.mutation_mode == "mutation"
    assert rec.recommended_profile == "codex-native-executor"
    assert rec.fallback_reason is None
    assert len(rec.rankings) == 2
    assert rec.rankings[0].profile == "codex-native-executor"
    assert rec.rankings[0].rank == 1
    assert rec.rankings[1].profile == "antigravity-native-executor"
    assert rec.rankings[1].rank == 2


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
    assert data["status"] == "complete"

    md_text = md_out.read_text(encoding="utf-8")
    assert "M29 Provider Reliability Advisory Report" in md_text
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


def test_default_independent_review_executes(tmp_path: Path) -> None:
    """Ensure completed mutation task defaults to independent review applicable and passed."""
    db_path = tmp_path / "test_review_default.db"
    db_url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)

    task = _create_task(
        task_id="default-review",
        status=TaskStatus.COMPLETED,
        verification_cmds=["pytest"],
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
    assert cell.stage_outcome_rates.review_pass_rate == 1.0


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

    for idx in range(10):
        t = _create_task(
            task_id=f"codex-{idx}",
            status=TaskStatus.COMPLETED,
            profile="codex-native-executor",
        )
        with Session(engine) as session:
            session.add(t)
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

    task = _create_task(
        task_id="multi-run-task",
        status=TaskStatus.FAILED,
    )
    task.worker_runs.clear()
    run_later = WorkerRun(
        id="run-b",
        worker_type=WorkerType.CODEX,
        worker_profile="codex-native-executor",
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        started_at=task.created_at + timedelta(minutes=5),
        finished_at=task.created_at + timedelta(minutes=8),
        status=WorkerRunStatus.FAILURE,
        verifier_outcome={"status": "failed", "failure_kind": "latest_failure"},
    )
    run_earlier = WorkerRun(
        id="run-a",
        worker_type=WorkerType.CODEX,
        worker_profile="codex-native-executor",
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        started_at=task.created_at + timedelta(minutes=1),
        finished_at=task.created_at + timedelta(minutes=3),
        status=WorkerRunStatus.FAILURE,
        verifier_outcome={"status": "failed", "failure_kind": "earlier_failure"},
    )
    task.worker_runs.extend([run_later, run_earlier])
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
