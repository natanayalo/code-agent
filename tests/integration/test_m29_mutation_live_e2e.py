"""End-to-end integration tests for M29 live adapters, watchdog, and evaluation runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
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
from evaluation.m29_mutation_dispatcher import HttpStackCandidateDispatcher
from evaluation.m29_mutation_fixture import (
    PINNED_TASKBOARD_FIXTURE_REVISION,
    clone_case_workspace_from_fixture,
    create_taskboard_fixture_repo,
)
from evaluation.m29_mutation_oracles import compute_verifier_source_sha256
from evaluation.m29_mutation_publication import reconcile_and_publish_mutation_wave
from evaluation.m29_mutation_reviewer_client import OpenRouterNeutralReviewerClient
from evaluation.m29_mutation_runner import M29MutationRunner
from evaluation.m29_mutation_runner_models import RawCaseManifestEntry
from evaluation.m29_mutation_runner_preflight import (
    default_mutation_constraints,
    default_mutation_task_spec,
)
from evaluation.m29_mutation_smokes import ExtractorStageSmokeResult
from evaluation.m29_mutation_suite import (
    M29MutationCase,
    M29MutationSuite,
    build_m29_mutation_suite,
)
from evaluation.m29_mutation_verifier import ReadOnlyExternalEvaluator
from evaluation.m29_mutation_watchdog import EvaluationRetentionWatchdog
from orchestrator.state import MemoryContext
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle, WorkspaceManager
from scripts.e2e.run_m29_mutation_wave import _save_run_results
from tests.unit.test_m29_mutation_fixture_and_verifier import REFERENCE_SOLUTIONS


def _make_valid_smoke_result() -> ExtractorStageSmokeResult:
    return ExtractorStageSmokeResult(
        total_smoke_tasks=20,
        candidate_profiles=("codex-native-executor", "antigravity-native-executor"),
        all_verification_inapplicable=True,
        all_review_inapplicable=True,
        report_verification_pass_rates={},
        report_review_pass_rates={},
        no_review_artifacts=True,
        status_and_events_unchanged=True,
        is_valid=True,
        violations=[],
    )


def _build_task_snapshot(
    task_id: str, ws_id: str, case: M29MutationCase, task_text: str
) -> dict[str, Any]:
    is_codex = "codex" in case.worker_profile
    return {
        "task_id": task_id,
        "session_id": "sess-1",
        "status": "completed",
        "task_text": task_text,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "task_spec": default_mutation_task_spec(case).model_dump(mode="json"),
        "constraints": default_mutation_constraints(),
        "latest_run": {
            "run_id": f"run-{task_id}",
            "worker_type": "codex" if is_codex else "antigravity",
            "workspace_id": ws_id,
            "status": "completed",
            "started_at": datetime.now(UTC).isoformat(),
            "budget_usage": {
                "native_agent": {
                    "model_execution": {
                        "provider": "codex" if is_codex else "antigravity",
                        "model": "gpt-6-luna" if is_codex else "gemini-3.8-flash",
                        "reasoning_effort": "high" if is_codex else "medium",
                    }
                }
            },
        },
        "pending_interactions": [],
        "timeline": [],
    }


def _create_mock_stack_client(
    fixture_dir: Path,
    ws_root: Path,
    watchdog: EvaluationRetentionWatchdog,
    suite: M29MutationSuite,
) -> httpx.Client:
    def api_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "chat/completions" in path:
            return httpx.Response(
                status_code=200,
                json={
                    "model": "anthropic/claude-3-7-sonnet",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"passed": True, "blocking_findings": []})
                            }
                        }
                    ],
                },
            )

        if request.method == "POST" and path == "/tasks":
            idx = len(watchdog.records)
            task_id = f"task-{idx + 1}"
            ws_id = f"ws-{task_id}"
            ws_path = ws_root / ws_id
            clone_case_workspace_from_fixture(
                fixture_dir, ws_path, expected_revision=PINNED_TASKBOARD_FIXTURE_REVISION
            )
            case = suite.cases[idx]
            target_path = ws_path / case.allowed_source_path
            target_path.write_text(REFERENCE_SOLUTIONS[case.topic], encoding="utf-8")
            data = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                status_code=202,
                json=_build_task_snapshot(task_id, ws_id, case, data.get("task_text", "")),
            )

        # GET /tasks/{task_id}
        parts = path.strip("/").split("/")
        task_id = parts[-1]
        task_num = int(task_id.split("-")[-1])
        case = suite.cases[task_num - 1]
        return httpx.Response(
            status_code=200,
            json=_build_task_snapshot(task_id, f"ws-{task_id}", case, case.prompt),
        )

    return httpx.Client(transport=httpx.MockTransport(api_handler))


def _seed_e2e_db_task(session: Session, raw: RawCaseManifestEntry) -> None:
    is_codex = "codex" in raw.worker_profile
    t_snap = raw.terminal_snapshot
    raw_ts = (
        datetime.fromisoformat(t_snap.terminal_timestamp)
        if (t_snap and t_snap.terminal_timestamp)
        else datetime.now(UTC)
    )
    if raw_ts.tzinfo is None:
        raw_ts = raw_ts.replace(tzinfo=UTC)

    task = Task(
        id=raw.task_id,
        session_id=f"session-{raw.task_id}",
        task_text=f"Mutation task {raw.task_id}",
        repo_url="https://github.invalid/m29/disposable-fixture",
        branch="master",
        status=TaskStatus.COMPLETED,
        chosen_worker=WorkerType.CODEX if is_codex else WorkerType.ANTIGRAVITY,
        chosen_profile=raw.worker_profile,
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
        created_at=raw_ts - timedelta(minutes=5),
        updated_at=raw_ts,
    )
    model_meta = (
        {"provider": "codex", "model": "gpt-6-luna", "reasoning_effort": "high"}
        if is_codex
        else {"provider": "antigravity", "model": "gemini-3.8-flash", "reasoning_effort": "medium"}
    )
    run = WorkerRun(
        id=f"run-{raw.task_id}",
        task_id=raw.task_id,
        worker_type=WorkerType.CODEX if is_codex else WorkerType.ANTIGRAVITY,
        worker_profile=raw.worker_profile,
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        orchestration_runtime=OrchestrationRuntime.TEMPORAL,
        started_at=raw_ts - timedelta(minutes=5),
        finished_at=raw_ts,
        status=WorkerRunStatus.SUCCESS,
        verifier_outcome={"status": "passed"},
        budget_usage={"tokens": 100, "native_agent": {"model_execution": model_meta}},
        artifact_index=[],
    )
    task.worker_runs.append(run)
    event = TaskTimelineEvent(
        id=f"evt-{raw.task_id}",
        task_id=raw.task_id,
        event_type="task_completed",
        created_at=raw_ts,
        payload={},
    )
    task.timeline_events.append(event)
    session.add(task)


def _assert_publication_flow(
    bundle_dir: Path,
    db_url: str,
    fixture_dir: Path,
    suite: M29MutationSuite,
    verifier_sha: str,
    as_of: datetime,
    result: Any,
) -> None:
    """Seed tasks, save run results, and verify post-cutoff publication flow."""
    with Session(create_engine(db_url)) as session:
        for raw in result.raw_entries:
            _seed_e2e_db_task(session, raw)
        session.commit()

    bundle_data = {
        "suite_name": suite.suite_name,
        "status": "completed",
        "initialized_at": datetime.now(UTC).isoformat(),
        "fixture_repo_path": str(fixture_dir),
        "fixture_revision": suite.target_repository_revision,
        "verifier_source_sha256": verifier_sha,
        "build_sha": "0123456789abcdef",
    }
    _save_run_results(bundle_dir, bundle_data, result)
    assert "task_cohort_snapshot" in bundle_data

    output_dir = bundle_dir / "published"
    artifacts = reconcile_and_publish_mutation_wave(
        bundle_dir=bundle_dir,
        db_url=db_url,
        output_dir=output_dir,
        now=as_of + timedelta(seconds=1),
    )
    assert len(artifacts.written_files) == 9
    assert (output_dir / "m29_provider_reliability_mutation_manifest.json").exists()
    assert (output_dir / "m29_provider_reliability_mutation_paired_analysis.json").exists()
    assert (output_dir / "m29_provider_reliability_report.json").exists()
    assert (output_dir / "m29_provider_reliability_operational_report.json").exists()
    assert (output_dir / "m29_provider_reliability_robustness_report.json").exists()


def _assert_watchdog_sweep(
    ws_root: Path,
    watchdog: EvaluationRetentionWatchdog,
    mgr: WorkspaceManager,
    policy: WorkspaceCleanupPolicy,
) -> None:
    """Record an uncleaned workspace with past deadline and verify sweep."""
    now = datetime.now(UTC)
    expired_ws = ws_root / "ws-expired-test"
    expired_ws.mkdir()
    h_exp = WorkspaceHandle(
        workspace_id="ws-expired-test",
        task_id="task-exp-test",
        workspace_path=expired_ws,
        repo_path=expired_ws,
        repo_url="",
        cleanup_policy=policy,
    )
    watchdog.record_workspace("case-exp", "task-exp-test", h_exp, now=now - timedelta(hours=80))
    sweep_res = watchdog.sweep_expired(mgr, now=now)
    assert sweep_res.expired_count == 1
    assert sweep_res.cleaned_count == 1
    assert not expired_ws.exists()


def test_live_adapters_e2e_stack_with_watchdog(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "taskboard_fixture"
    create_taskboard_fixture_repo(fixture_dir)

    ws_root = tmp_path / "workspaces"
    ws_root.mkdir(parents=True)
    bundle_dir = tmp_path / "bundle"
    policy = WorkspaceCleanupPolicy()
    mgr = WorkspaceManager(root_dir=ws_root, cleanup_policy=policy)

    suite = build_m29_mutation_suite()
    watchdog = EvaluationRetentionWatchdog(bundle_dir, retention_seconds=3600)
    mock_client = _create_mock_stack_client(fixture_dir, ws_root, watchdog, suite)

    db_path = tmp_path / "test_runner.db"
    db_url = f"sqlite:///{db_path}"
    Base.metadata.create_all(create_engine(db_url))

    dispatcher = HttpStackCandidateDispatcher(
        api_base_url="http://test-api",
        db_url=db_url,
        workspace_root=ws_root,
        retention_watchdog=watchdog,
        httpx_client=mock_client,
    )
    reviewer = OpenRouterNeutralReviewerClient(
        api_key="mock-key",
        httpx_client=mock_client,
    )
    evaluator = ReadOnlyExternalEvaluator(reviewer_client=reviewer)
    verifier_sha = compute_verifier_source_sha256()
    as_of = datetime.now(UTC) + timedelta(minutes=5)

    runner = M29MutationRunner(
        suite=suite,
        db_url=db_url,
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=_make_valid_smoke_result(),
        fixture_repo_path=fixture_dir,
        expected_verifier_sha256=verifier_sha,
        as_of=as_of,
        retention_watchdog=watchdog,
    )
    result = runner.run()

    assert result.status == "completed"
    assert len(result.raw_entries) == 20
    assert len(result.workspaces_cleaned) == 20
    assert len(watchdog.records) == 20
    assert all(r.cleaned_up for r in watchdog.records)

    _assert_publication_flow(bundle_dir, db_url, fixture_dir, suite, verifier_sha, as_of, result)
    _assert_watchdog_sweep(ws_root, watchdog, mgr, policy)


def test_cli_preflight_error_handling_and_stale_baseline_cleanup(
    tmp_path: Path, monkeypatch: Any
) -> None:
    fixture_dir = tmp_path / "fixture"
    create_taskboard_fixture_repo(fixture_dir)
    bundle_dir = tmp_path / "bundle_preflight"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "bundle.json").write_text('{"status": "initialized"}', encoding="utf-8")
    db_path = tmp_path / "preflight.db"
    db_url = f"sqlite:///{db_path}"
    Base.metadata.create_all(create_engine(db_url))
    ws_root = tmp_path / "ws_preflight"
    ws_root.mkdir(parents=True)

    from evaluation.m29_mutation_runner_preflight import TaskCohortSnapshot
    from scripts.e2e.run_m29_mutation_wave import main as cli_main

    now = datetime.now(UTC)
    dummy_snap = TaskCohortSnapshot(
        as_of=now,
        window_start_at=now - timedelta(days=90),
        window_end_at=now,
        baseline_memory_context_hash="0" * 64,
        baseline_ids_by_profile={"codex-native-executor": (), "antigravity-native-executor": ()},
        frozen_baseline_ids_sha256_by_profile={
            "codex-native-executor": "0" * 64,
            "antigravity-native-executor": "0" * 64,
        },
    )
    monkeypatch.setattr(
        "scripts.e2e.run_m29_mutation_wave.run_mutation_preflight_checks",
        lambda **kwargs: dummy_snap,
    )

    # 1. Invalid --as-of returns exit code 1
    rc_invalid_as_of = cli_main(
        [
            "preflight",
            "--bundle-dir",
            str(bundle_dir),
            "--workspace-root",
            str(ws_root),
            "--fixture-repo-path",
            str(fixture_dir),
            "--db-url",
            db_url,
            "--as-of",
            "not-a-valid-datetime",
            "--ack-disposable-mutation-stack",
        ]
    )
    assert rc_invalid_as_of == 1

    # 2. Re-running preflight on empty DB removes stale baseline_report.json
    stale_baseline = bundle_dir / "baseline_report.json"
    stale_baseline.write_text('{"stale": true}', encoding="utf-8")
    rc_valid = cli_main(
        [
            "preflight",
            "--bundle-dir",
            str(bundle_dir),
            "--workspace-root",
            str(ws_root),
            "--fixture-repo-path",
            str(fixture_dir),
            "--db-url",
            db_url,
            "--ack-disposable-mutation-stack",
        ]
    )
    assert rc_valid == 0
    assert not stale_baseline.exists()
