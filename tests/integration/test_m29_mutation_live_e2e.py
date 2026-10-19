"""End-to-end integration tests for M29 live adapters, watchdog, and evaluation runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine

from db.base import Base
from evaluation.m29_mutation_dispatcher import HttpStackCandidateDispatcher
from evaluation.m29_mutation_fixture import (
    PINNED_TASKBOARD_FIXTURE_REVISION,
    clone_case_workspace_from_fixture,
    create_taskboard_fixture_repo,
)
from evaluation.m29_mutation_oracles import compute_verifier_source_sha256
from evaluation.m29_mutation_reviewer_client import OpenRouterNeutralReviewerClient
from evaluation.m29_mutation_runner import M29MutationRunner
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
        retention_watchdog=watchdog,
    )
    result = runner.run()

    assert result.status == "completed"
    assert len(result.raw_entries) == 20
    assert len(result.workspaces_cleaned) == 20
    assert len(watchdog.records) == 20
    assert all(r.cleaned_up for r in watchdog.records)

    # Test retention watchdog sweep: record an uncleaned workspace with past deadline
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
