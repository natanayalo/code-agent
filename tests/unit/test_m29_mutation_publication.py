"""Unit tests for M29 mutation publication, baseline extraction, and operator CLI."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.base import Base
from db.models import (
    OrchestrationRuntime,
    Task,
    TaskStatus,
    TaskTimelineEvent,
    WorkerRun,
    WorkerRunStatus,
    WorkerRuntimeMode,
    WorkerType,
)
from evaluation.m29_mutation_manifest import (
    M29MutationManifestCase,
    assert_sanitized_mutation_manifest,
    assert_sanitized_mutation_paired_report,
)
from evaluation.m29_mutation_publication import (
    _parse_utc_datetime,
    extract_mutation_baseline_snapshot,
    reconcile_and_publish_mutation_wave,
)
from evaluation.m29_mutation_runner_archive import validate_sanitized_archive
from evaluation.m29_mutation_suite import build_m29_mutation_suite
from scripts.e2e.run_m29_mutation_wave import main as cli_main

EMPTY_IDS_SHA256 = hashlib.sha256(b"[]").hexdigest()


def _seed_db_task(
    session: Session,
    task_id: str,
    profile: str,
    created_at: datetime,
    finished_at: datetime,
    status: TaskStatus = TaskStatus.COMPLETED,
    failure_kind: str | None = None,
) -> Task:
    is_codex = "codex" in profile
    task = Task(
        id=task_id,
        session_id=f"session-{task_id}",
        task_text=f"Task {task_id}",
        repo_url="https://github.invalid/m29/disposable-fixture",
        branch="master",
        status=status,
        chosen_worker=WorkerType.CODEX if is_codex else WorkerType.ANTIGRAVITY,
        chosen_profile=profile,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        task_spec={
            "task_type": "feature",
            "allowed_actions": ["modify_workspace_files"],
            "verification_commands": [],
            "delivery_mode": "workspace",
        },
        constraints={
            "skip_independent_review": True,
            "post_terminal_quality_evaluation": True,
        },
        created_at=created_at,
        updated_at=finished_at,
    )
    model_meta = (
        {"provider": "codex", "model": "gpt-6-luna", "reasoning_effort": "high"}
        if is_codex
        else {"provider": "antigravity", "model": "gemini-3.8-flash", "reasoning_effort": "medium"}
    )
    run_status = (
        WorkerRunStatus.SUCCESS if status == TaskStatus.COMPLETED else WorkerRunStatus.FAILURE
    )
    outcome = (
        {"status": "passed"}
        if status == TaskStatus.COMPLETED
        else {"status": "failed", "failure_kind": failure_kind or "compile_error"}
    )
    run = WorkerRun(
        id=f"run-{task_id}",
        task_id=task_id,
        worker_type=WorkerType.CODEX if is_codex else WorkerType.ANTIGRAVITY,
        worker_profile=profile,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        started_at=created_at,
        finished_at=finished_at,
        status=run_status,
        verifier_outcome=outcome,
        budget_usage={"tokens": 100, "native_agent": {"model_execution": model_meta}},
        artifact_index=[],
    )
    task.worker_runs.append(run)
    event_type = "task_completed" if status == TaskStatus.COMPLETED else "task_failed"
    event = TaskTimelineEvent(
        id=f"evt-{task_id}",
        task_id=task_id,
        event_type=event_type,
        created_at=finished_at,
        payload={},
    )
    task.timeline_events.append(event)
    session.add(task)
    return task


def _build_case_raw_and_sanitized(
    tmp_path: Path,
    case: Any,
    task_id: str,
    finished_at: datetime,
) -> tuple[dict[str, Any], M29MutationManifestCase]:
    provider = "codex" if "codex" in case.worker_profile else "antigravity"
    raw_entry = {
        "case_id": case.case_id,
        "task_id": task_id,
        "workspace_id": f"ws-{task_id}",
        "workspace_path": str(tmp_path / f"ws-{task_id}"),
        "trusted_git_dir": None,
        "pair_group": case.pair_group,
        "pair_order": case.pair_order,
        "topic": case.topic,
        "worker_profile": case.worker_profile,
        "provider": provider,
        "target_repository_revision": case.target_repository_revision,
        "terminal_snapshot": {
            "task_status": "completed",
            "terminal_event_type": "task_completed",
            "terminal_timestamp": finished_at.isoformat(),
            "failure_kind": None,
            "diff_sha256": "0" * 64,
            "changed_files": [case.allowed_source_path],
            "checksum": "1" * 64,
        },
        "candidate_identity": {
            "provider": provider,
            "model": "gpt-6-luna" if provider == "codex" else "gemini-3.8-flash",
            "reasoning_effort": "high" if provider == "codex" else "medium",
        },
        "candidate_identity_status": "verified",
        "time_to_terminal_seconds": 300.0,
        "failure_kind": None,
        "diff_sha256": "0" * 64,
        "raw_diff": "sample diff",
        "changed_files": [case.allowed_source_path],
        "external_evaluation": None,
        "cleaned_up": True,
        "cleanup_error": None,
        "stop_reason": None,
        "case_outcome": "completed_quality_pass",
    }
    sanitized_case = M29MutationManifestCase(
        case_id=case.case_id,
        pair_group=case.pair_group,
        pair_order=case.pair_order,
        topic=case.topic,
        worker_profile=case.worker_profile,
        provider=provider,
        terminal_status="completed",
        accepted=True,
        time_to_terminal_seconds=300.0,
        execution_identity_status="verified",
        identity_matches=True,
        quality_status="pass",
        acceptance_oracle_passed=True,
        regression_suite_passed=True,
        neutral_review_passed=True,
        evaluator_identity_status="verified",
        evaluator_identity_matches=True,
    )
    return raw_entry, sanitized_case


def _build_test_bundle_data(
    suite: Any,
    tmp_path: Path,
    as_of: datetime,
    archive_checksum: str,
) -> dict[str, Any]:
    return {
        "suite_name": suite.suite_name,
        "status": "completed",
        "initialized_at": (as_of - timedelta(days=3)).isoformat(),
        "finished_at": (as_of - timedelta(hours=1)).isoformat(),
        "fixture_repo_path": str(tmp_path / "fixture"),
        "fixture_revision": suite.target_repository_revision,
        "verifier_source_sha256": "a" * 64,
        "build_sha": "0123456789abcdef",
        "case_count": 20,
        "sanitized_case_count": 20,
        "sanitized_archive_checksum": archive_checksum,
        "task_cohort_snapshot": {
            "as_of": as_of.isoformat(),
            "window_start_at": (as_of - timedelta(days=90)).isoformat(),
            "window_end_at": as_of.isoformat(),
            "baseline_memory_context_hash": "b" * 64,
            "baseline_ids_by_profile": {
                "codex-native-executor": [],
                "antigravity-native-executor": [],
            },
            "frozen_baseline_ids_sha256_by_profile": {
                "codex-native-executor": EMPTY_IDS_SHA256,
                "antigravity-native-executor": EMPTY_IDS_SHA256,
            },
        },
    }


def _prepare_20_case_bundle(
    tmp_path: Path,
    session: Session,
    as_of: datetime,
) -> tuple[Path, list[dict[str, Any]], list[M29MutationManifestCase]]:
    suite = build_m29_mutation_suite()
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    raw_entries: list[dict[str, Any]] = []
    sanitized_cases: list[M29MutationManifestCase] = []

    for idx, case in enumerate(suite.cases, start=1):
        task_id = f"task-case-{idx:02d}"
        created_at = as_of - timedelta(days=2, minutes=idx * 15)
        finished_at = created_at + timedelta(minutes=5)
        _seed_db_task(session, task_id, case.worker_profile, created_at, finished_at)

        raw_entry, san_case = _build_case_raw_and_sanitized(tmp_path, case, task_id, finished_at)
        raw_entries.append(raw_entry)
        sanitized_cases.append(san_case)

    session.commit()
    archive_checksum = validate_sanitized_archive(sanitized_cases)
    bundle_data = _build_test_bundle_data(suite, tmp_path, as_of, archive_checksum)

    (bundle_dir / "bundle.json").write_text(json.dumps(bundle_data, indent=2), encoding="utf-8")
    (bundle_dir / "raw_entries.json").write_text(
        json.dumps(raw_entries, indent=2), encoding="utf-8"
    )
    (bundle_dir / "sanitized_cases.json").write_text(
        json.dumps([c.model_dump(mode="json") for c in sanitized_cases], indent=2),
        encoding="utf-8",
    )
    return bundle_dir, raw_entries, sanitized_cases


def test_extract_mutation_baseline_snapshot_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    baseline_ids, report = extract_mutation_baseline_snapshot(db_url, as_of)

    assert baseline_ids["codex-native-executor"] == ()
    assert baseline_ids["antigravity-native-executor"] == ()
    assert report.policy.as_of == as_of


def test_extract_mutation_baseline_snapshot_with_tasks(tmp_path: Path) -> None:
    db_path = tmp_path / "baseline.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    with Session(engine) as session:
        t1 = _seed_db_task(
            session,
            "base-codex-1",
            "codex-native-executor",
            as_of - timedelta(days=10),
            as_of - timedelta(days=10, minutes=-5),
        )
        t2 = _seed_db_task(
            session,
            "base-ag-1",
            "antigravity-native-executor",
            as_of - timedelta(days=5),
            as_of - timedelta(days=5, minutes=-5),
        )
        t1_id = str(t1.id)
        t2_id = str(t2.id)
        session.commit()

    baseline_ids, report = extract_mutation_baseline_snapshot(db_url, as_of)
    assert baseline_ids["codex-native-executor"] == (t1_id,)
    assert baseline_ids["antigravity-native-executor"] == (t2_id,)
    cells = {c.profile: c for c in report.evidence_cells if c.mutation_mode == "mutation"}
    assert cells["codex-native-executor"].sample_size == 1
    assert cells["antigravity-native-executor"].sample_size == 1


def test_reconcile_and_publish_mutation_wave_happy_path(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    now = as_of + timedelta(minutes=10)

    with Session(engine) as session:
        bundle_dir, _raw, _san = _prepare_20_case_bundle(tmp_path, session, as_of)

    output_dir = tmp_path / "published"
    artifacts = reconcile_and_publish_mutation_wave(
        bundle_dir=bundle_dir,
        db_url=db_url,
        output_dir=output_dir,
        now=now,
    )

    expected_files = {
        "m29_provider_reliability_mutation_manifest.json",
        "m29_provider_reliability_mutation_paired_analysis.json",
        "m29_provider_reliability_mutation_paired_analysis.md",
        "m29_provider_reliability_report.json",
        "m29_provider_reliability_report.md",
        "m29_provider_reliability_operational_report.json",
        "m29_provider_reliability_operational_report.md",
        "m29_provider_reliability_robustness_report.json",
        "m29_provider_reliability_robustness_report.md",
    }
    actual_files = {p.name for p in artifacts.written_files}
    assert actual_files == expected_files

    manifest_json = (output_dir / "m29_provider_reliability_mutation_manifest.json").read_text(
        encoding="utf-8"
    )
    assert_sanitized_mutation_manifest(manifest_json)

    paired_json = (output_dir / "m29_provider_reliability_mutation_paired_analysis.json").read_text(
        encoding="utf-8"
    )
    assert_sanitized_mutation_paired_report(paired_json)


def test_reconcile_and_publish_fails_before_cutoff(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    now = as_of - timedelta(minutes=1)

    with Session(engine) as session:
        bundle_dir, _raw, _san = _prepare_20_case_bundle(tmp_path, session, as_of)

    with pytest.raises(ValueError, match="is before cutoff"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir,
            db_url=db_url,
            output_dir=tmp_path / "published",
            now=now,
        )


def test_reconcile_and_publish_fails_if_bundle_not_completed(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    now = as_of + timedelta(minutes=10)

    with Session(engine) as session:
        bundle_dir, _raw, _san = _prepare_20_case_bundle(tmp_path, session, as_of)

    bundle_path = bundle_dir / "bundle.json"
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    data["status"] = "halted"
    bundle_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="must be 'completed' or 'published'"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir,
            db_url=db_url,
            output_dir=tmp_path / "published",
            now=now,
        )


def test_reconcile_and_publish_fails_if_checksum_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    now = as_of + timedelta(minutes=10)

    with Session(engine) as session:
        bundle_dir, _raw, _san = _prepare_20_case_bundle(tmp_path, session, as_of)

    bundle_path = bundle_dir / "bundle.json"
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    data["sanitized_archive_checksum"] = "0" * 64
    bundle_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="sanitized archive checksum mismatch"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir,
            db_url=db_url,
            output_dir=tmp_path / "published",
            now=now,
        )


def test_reconcile_and_publish_fails_if_task_missing_in_db(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    now = as_of + timedelta(minutes=10)

    with Session(engine) as session:
        bundle_dir, _raw, _san = _prepare_20_case_bundle(tmp_path, session, as_of)
        # Delete one task from the DB
        task = session.get(Task, "task-case-01")
        if task:
            for ev in list(task.timeline_events):
                session.delete(ev)
            for run in list(task.worker_runs):
                session.delete(run)
            session.delete(task)
            session.commit()

    with pytest.raises(ValueError, match="not found in canonical database snapshot"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir,
            db_url=db_url,
            output_dir=tmp_path / "published",
            now=now,
        )


def test_cli_publish_requires_ack_disposable_stack(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    ret = cli_main(["publish", "--bundle-dir", str(bundle_dir), "--db-url", "sqlite:///test.db"])
    assert ret == 2


def test_cli_publish_happy_path(tmp_path: Path) -> None:
    db_path = tmp_path / "cli_test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime.now(UTC) - timedelta(minutes=5)

    with Session(engine) as session:
        bundle_dir, _raw, _san = _prepare_20_case_bundle(tmp_path, session, as_of)

    ret = cli_main(
        [
            "publish",
            "--bundle-dir",
            str(bundle_dir),
            "--db-url",
            db_url,
            "--ack-disposable-mutation-stack",
        ]
    )
    assert ret == 0

    bundle_data = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    assert bundle_data["status"] == "published"
    assert "manifest_sha256" in bundle_data
    assert "published_at" in bundle_data
    assert (bundle_dir / "published" / "m29_provider_reliability_report.json").exists()


def test_resolve_build_sha_validation() -> None:
    from evaluation.m29_mutation_publication import _resolve_build_sha

    assert _resolve_build_sha("0123456789abcdef", None) == "0123456789abcdef"
    assert _resolve_build_sha(None, "abcdef012345") == "abcdef012345"

    with pytest.raises(ValueError, match="invalid build_sha"):
        _resolve_build_sha("not_a_valid_hex_sha!", None)


def test_bundle_validation_missing_files_and_counts(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

    # Missing bundle.json
    with pytest.raises(FileNotFoundError, match="bundle.json not found"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url="sqlite:///:memory:", output_dir=tmp_path / "out", now=now
        )

    (bundle_dir / "bundle.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="raw_entries.json not found"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url="sqlite:///:memory:", output_dir=tmp_path / "out", now=now
        )

    (bundle_dir / "raw_entries.json").write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="sanitized_cases.json not found"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url="sqlite:///:memory:", output_dir=tmp_path / "out", now=now
        )

    (bundle_dir / "sanitized_cases.json").write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError, match="raw_entries count mismatch"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url="sqlite:///:memory:", output_dir=tmp_path / "out", now=now
        )


def test_reconcile_fails_on_profile_or_status_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "drift.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    now = as_of + timedelta(minutes=10)

    with Session(engine) as session:
        bundle_dir, _raw, _san = _prepare_20_case_bundle(tmp_path, session, as_of)
        # Mutate one task profile and model execution in DB to trigger profile mismatch
        task = session.get(Task, "task-case-01")
        if task:
            new_profile = (
                "antigravity-native-executor"
                if task.chosen_profile == "codex-native-executor"
                else "codex-native-executor"
            )
            task.chosen_profile = new_profile
            for r in task.worker_runs:
                r.worker_profile = new_profile
                r.budget_usage = {
                    "tokens": 100,
                    "native_agent": {
                        "model_execution": {
                            "provider": "antigravity" if "antigravity" in new_profile else "codex",
                            "model": (
                                "gemini-3.8-flash" if "antigravity" in new_profile else "gpt-6-luna"
                            ),
                            "reasoning_effort": (
                                "medium" if "antigravity" in new_profile else "high"
                            ),
                        }
                    },
                }
            session.commit()

    with pytest.raises(ValueError, match="profile mismatch"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir,
            db_url=db_url,
            output_dir=tmp_path / "out",
            now=now,
        )


def test_reconcile_with_baseline_report_file(tmp_path: Path) -> None:
    db_path = tmp_path / "baseline_file.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    now = as_of + timedelta(minutes=10)

    # Extract baseline report before wave tasks are inserted into DB
    _, rep = extract_mutation_baseline_snapshot(db_url, as_of)

    with Session(engine) as session:
        bundle_dir, _raw, _san = _prepare_20_case_bundle(tmp_path, session, as_of)

    # Save baseline_report.json into bundle_dir
    from evaluation.provider_reliability_report import render_json_report

    (bundle_dir / "baseline_report.json").write_text(render_json_report(rep), encoding="utf-8")

    artifacts = reconcile_and_publish_mutation_wave(
        bundle_dir=bundle_dir,
        db_url=db_url,
        output_dir=tmp_path / "out_baseline",
        now=now,
    )
    assert len(artifacts.written_files) == 9


def test_resolve_build_sha_git_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from evaluation.m29_mutation_publication import _parse_utc_datetime, _resolve_build_sha

    # Test timezone-naive parsing
    dt = _parse_utc_datetime("2026-10-20T12:00:00")
    assert dt.tzinfo == UTC

    # Test git fallback
    sha = _resolve_build_sha(None, None)
    assert re.match(r"^[0-9a-f]{7,64}$", sha)

    # Test fallback exception
    def mock_check_output(*_args: Any, **_kwargs: Any) -> bytes:
        raise RuntimeError("git failed")

    monkeypatch.setattr(subprocess, "check_output", mock_check_output)
    fallback_sha = _resolve_build_sha(None, None)
    assert fallback_sha == "0" * 40


def test_reconcile_additional_validation_rejections(tmp_path: Path) -> None:
    db_path = tmp_path / "checks.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)

    as_of = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
    now = as_of + timedelta(minutes=10)

    with Session(engine) as session:
        bundle_dir, raw_entries, sanitized_cases = _prepare_20_case_bundle(tmp_path, session, as_of)

    # 1. Sanitized case count mismatch
    san_file = bundle_dir / "sanitized_cases.json"
    san_data = json.loads(san_file.read_text(encoding="utf-8"))
    san_file.write_text(json.dumps(san_data[:10]), encoding="utf-8")
    with pytest.raises(ValueError, match="sanitized_cases count mismatch"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url=db_url, output_dir=tmp_path / "out1", now=now
        )
    # Restore sanitized file
    san_file.write_text(json.dumps(san_data), encoding="utf-8")

    # 2. Terminal timestamp after cutoff
    raw_file = bundle_dir / "raw_entries.json"
    raw_data = json.loads(raw_file.read_text(encoding="utf-8"))
    orig_ts = raw_data[0]["terminal_snapshot"]["terminal_timestamp"]
    raw_data[0]["terminal_snapshot"]["terminal_timestamp"] = (as_of + timedelta(days=1)).isoformat()
    raw_file.write_text(json.dumps(raw_data), encoding="utf-8")
    with pytest.raises(ValueError, match="is after frozen cutoff"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url=db_url, output_dir=tmp_path / "out2", now=now
        )
    # Restore timestamp
    raw_data[0]["terminal_snapshot"]["terminal_timestamp"] = orig_ts
    raw_file.write_text(json.dumps(raw_data), encoding="utf-8")

    # 3. Terminal timestamp drift between DB and raw archive
    raw_data[0]["terminal_snapshot"]["terminal_timestamp"] = (
        _parse_utc_datetime(orig_ts) - timedelta(minutes=5)
    ).isoformat()
    raw_file.write_text(json.dumps(raw_data), encoding="utf-8")
    with pytest.raises(ValueError, match="terminal timestamp drift"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url=db_url, output_dir=tmp_path / "out3", now=now
        )
    # Restore
    raw_data[0]["terminal_snapshot"]["terminal_timestamp"] = orig_ts
    raw_file.write_text(json.dumps(raw_data), encoding="utf-8")

    # 4. Accepted mismatch between DB and manifest
    san_data_mut = json.loads(san_file.read_text(encoding="utf-8"))
    san_data_mut[0]["accepted"] = False
    san_data_mut[0]["terminal_status"] = "failed"
    san_data_mut[0]["failure_kind"] = "test_failure"
    san_data_mut[0]["report_failure_kind"] = "test_failure"
    cases_mut = [M29MutationManifestCase.model_validate(c) for c in san_data_mut]
    bundle_data_mut = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
    bundle_data_mut["sanitized_archive_checksum"] = validate_sanitized_archive(cases_mut)
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle_data_mut), encoding="utf-8")
    san_file.write_text(json.dumps(san_data_mut), encoding="utf-8")
    with pytest.raises(ValueError, match="accepted mismatch"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url=db_url, output_dir=tmp_path / "out4", now=now
        )

    # 5. Excluded case unexpectedly in canonical snapshot
    san_data_mut[0]["exclusion_reason"] = "environment_failure"
    cases_mut2 = [M29MutationManifestCase.model_validate(c) for c in san_data_mut]
    bundle_data_mut["sanitized_archive_checksum"] = validate_sanitized_archive(cases_mut2)
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle_data_mut), encoding="utf-8")
    san_file.write_text(json.dumps(san_data_mut), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpectedly included in canonical database snapshot"):
        reconcile_and_publish_mutation_wave(
            bundle_dir=bundle_dir, db_url=db_url, output_dir=tmp_path / "out5", now=now
        )
