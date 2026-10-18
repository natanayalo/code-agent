"""Unit tests for M29 live evaluation adapters, reviewer client, and retention watchdog."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.base import Base
from db.models import (
    MemoryObservation,
    PersonalMemory,
    ProjectMemory,
)
from evaluation.m29_mutation_dispatcher import (
    DisposableDbMemoryIsolationManager,
    HttpStackCandidateDispatcher,
)
from evaluation.m29_mutation_isolation import (
    build_evaluation_cleanup_policy,
)
from evaluation.m29_mutation_reviewer_client import OpenRouterNeutralReviewerClient
from evaluation.m29_mutation_suite import (
    DEFAULT_M29_NEUTRAL_EVALUATOR,
    NEUTRAL_EVALUATOR_SYSTEM_PROMPT,
    M29MutationCase,
    build_m29_mutation_suite,
)
from evaluation.m29_mutation_verifier import BlindedNeutralReviewRequest
from evaluation.m29_mutation_watchdog import (
    EvaluationRetentionWatchdog,
)
from evaluation.provider_reliability_models import ExecutionIdentity
from orchestrator.state import MemoryContext
from sandbox.workspace import WorkspaceHandle, WorkspaceManager
from scripts.e2e.run_m29_mutation_wave import main as cli_main


def _make_sample_case() -> M29MutationCase:
    suite = build_m29_mutation_suite()
    return suite.cases[0]


def _setup_test_db(db_url: str) -> None:
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)


class TestEvaluationRetentionWatchdog:
    """Tests for EvaluationRetentionWatchdog 72-hour tracking and sweeping."""

    def test_record_workspace_and_ledger_persistence(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        watchdog = EvaluationRetentionWatchdog(bundle_dir, retention_seconds=3600)

        ws_dir = tmp_path / "workspaces" / "ws-1"
        ws_dir.mkdir(parents=True)
        handle = WorkspaceHandle(
            workspace_id="ws-1",
            task_id="task-1",
            workspace_path=ws_dir,
            repo_path=ws_dir,
            repo_url="",
            cleanup_policy=build_evaluation_cleanup_policy(),
        )

        now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
        rec = watchdog.record_workspace("case-01", "task-1", handle, now=now)

        assert rec.workspace_id == "ws-1"
        assert rec.provisioned_at == now
        assert rec.retention_deadline_at == now + timedelta(seconds=3600)
        assert not rec.cleaned_up

        ledger_file = bundle_dir / "retention_ledger.json"
        assert ledger_file.exists()

        # Reload watchdog from disk
        watchdog_reloaded = EvaluationRetentionWatchdog(bundle_dir, retention_seconds=3600)
        assert len(watchdog_reloaded.records) == 1
        assert watchdog_reloaded.records[0].workspace_id == "ws-1"

    def test_mark_cleaned_success_and_error(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        watchdog = EvaluationRetentionWatchdog(bundle_dir, retention_seconds=3600)

        ws_dir = tmp_path / "workspaces" / "ws-2"
        ws_dir.mkdir(parents=True)
        handle = WorkspaceHandle(
            workspace_id="ws-2",
            task_id="task-2",
            workspace_path=ws_dir,
            repo_path=ws_dir,
            repo_url="",
            cleanup_policy=build_evaluation_cleanup_policy(),
        )
        watchdog.record_workspace("case-02", "task-2", handle)
        assert len(watchdog.uncleaned_records) == 1

        watchdog.mark_cleaned("ws-2", error="test cleanup error")
        assert len(watchdog.uncleaned_records) == 1
        assert watchdog.records[0].cleanup_error == "test cleanup error"
        assert not watchdog.records[0].cleaned_up

        watchdog.mark_cleaned("ws-2")
        assert len(watchdog.uncleaned_records) == 0
        assert watchdog.records[0].cleaned_up
        assert watchdog.records[0].cleanup_error is None

    def test_sweep_expired_skips_unexpired_and_cleans_expired(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir(parents=True)
        wm = WorkspaceManager(root_dir=ws_root, cleanup_policy=build_evaluation_cleanup_policy())

        watchdog = EvaluationRetentionWatchdog(bundle_dir, retention_seconds=3600)

        # ws-unexpired: provisioned at T0, deadline T0 + 1h
        t0 = datetime(2026, 10, 19, 10, 0, 0, tzinfo=UTC)
        ws_unexp = ws_root / "ws-unexp"
        ws_unexp.mkdir()
        h_unexp = WorkspaceHandle(
            workspace_id="ws-unexp",
            task_id="t-unexp",
            workspace_path=ws_unexp,
            repo_path=ws_unexp,
            repo_url="",
            cleanup_policy=build_evaluation_cleanup_policy(),
        )
        watchdog.record_workspace("case-1", "t-unexp", h_unexp, now=t0)

        # ws-expired: provisioned at T0 - 2h, deadline T0 - 1h
        ws_exp = ws_root / "ws-exp"
        ws_exp.mkdir()
        git_dir = ws_root / ".code-agent-git" / "ws-exp"
        git_dir.mkdir(parents=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        h_exp = WorkspaceHandle(
            workspace_id="ws-exp",
            task_id="t-exp",
            workspace_path=ws_exp,
            repo_path=ws_exp,
            repo_url="",
            cleanup_policy=build_evaluation_cleanup_policy(),
            trusted_git_dir=git_dir,
        )
        watchdog.record_workspace("case-2", "t-exp", h_exp, now=t0 - timedelta(hours=2))

        # Sweep at t0: ws-exp is expired, ws-unexp is not
        sweep_res = watchdog.sweep_expired(wm, now=t0)
        assert sweep_res.expired_count == 1
        assert sweep_res.cleaned_count == 1
        assert "ws-exp" in sweep_res.cleaned_workspace_ids
        assert not ws_exp.exists()
        assert not git_dir.exists()
        assert ws_unexp.exists()

        # Sweep with force_all=True cleans ws-unexp
        sweep_all = watchdog.sweep_expired(wm, now=t0, force_all=True)
        assert sweep_all.cleaned_count == 1
        assert "ws-unexp" in sweep_all.cleaned_workspace_ids
        assert not ws_unexp.exists()


class TestOpenRouterNeutralReviewerClient:
    """Tests for OpenRouterNeutralReviewerClient verification and blinded diff review."""

    def test_precondition_validation_failures(self) -> None:
        client = OpenRouterNeutralReviewerClient(api_key="test-key")
        req = BlindedNeutralReviewRequest(
            case_topic="sample",
            prompt="do stuff",
            acceptance_oracle="check it",
            allowed_source_path="src/a.py",
            diff_text="diff",
        )

        bad_sha_manifest = DEFAULT_M29_NEUTRAL_EVALUATOR.model_copy(
            update={"system_prompt_sha256": "0" * 64}
        )
        outcome = client.review_blinded_diff(req, bad_sha_manifest)
        assert not outcome.passed
        assert outcome.evaluator_identity_status == "unavailable"
        assert "system_prompt_sha256_mismatch" in str(outcome.reason)

    def test_missing_api_key_returns_unavailable(self) -> None:
        client = OpenRouterNeutralReviewerClient(api_key="")
        req = BlindedNeutralReviewRequest(
            case_topic="sample",
            prompt="do stuff",
            acceptance_oracle="check it",
            allowed_source_path="src/a.py",
            diff_text="diff",
        )
        outcome = client.review_blinded_diff(req, DEFAULT_M29_NEUTRAL_EVALUATOR)
        assert not outcome.passed
        assert outcome.evaluator_identity_status == "unavailable"
        assert outcome.reason == "missing_openrouter_api_key"

    def test_successful_review_pass(self) -> None:
        captured_payloads: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            data = json.loads(request.content.decode("utf-8"))
            captured_payloads.append(data)
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

        mock_transport = httpx.MockTransport(handler)
        client = OpenRouterNeutralReviewerClient(
            api_key="test-key",
            httpx_client=httpx.Client(transport=mock_transport),
        )

        req = BlindedNeutralReviewRequest(
            case_topic="sample",
            prompt="implement feature",
            acceptance_oracle="oracle text",
            allowed_source_path="src/a.py",
            diff_text="+line",
            changed_files=("src/a.py",),
        )

        outcome = client.review_blinded_diff(req, DEFAULT_M29_NEUTRAL_EVALUATOR)
        assert outcome.passed
        assert outcome.evaluator_identity_status == "verified"
        assert outcome.evaluator_identity == DEFAULT_M29_NEUTRAL_EVALUATOR.identity
        assert len(captured_payloads) == 1
        payload = captured_payloads[0]
        assert payload["provider"] == {"allow_fallbacks": False}
        assert payload["messages"][0]["content"] == NEUTRAL_EVALUATOR_SYSTEM_PROMPT
        # Check blinded prompt does not contain provider identities or case ids
        prompt_content = payload["messages"][1]["content"]
        assert "codex" not in prompt_content
        assert "antigravity" not in prompt_content

    def test_identity_mismatch_detected_fail_closed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=200,
                json={
                    "model": "openai/gpt-4o",  # Mismatched model returned!
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"passed": True, "blocking_findings": []})
                            }
                        }
                    ],
                },
            )

        client = OpenRouterNeutralReviewerClient(
            api_key="test-key",
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        req = BlindedNeutralReviewRequest(
            case_topic="sample",
            prompt="implement feature",
            acceptance_oracle="oracle",
            allowed_source_path="src/a.py",
            diff_text="+line",
        )
        outcome = client.review_blinded_diff(req, DEFAULT_M29_NEUTRAL_EVALUATOR)
        assert not outcome.passed
        assert outcome.evaluator_identity_status == "mismatched"
        assert outcome.fallback_attempted
        assert "evaluator_identity_mismatch" in str(outcome.reason)

    def test_http_error_handling(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=502, text="Bad Gateway")

        client = OpenRouterNeutralReviewerClient(
            api_key="test-key",
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        req = BlindedNeutralReviewRequest(
            case_topic="sample",
            prompt="goal",
            acceptance_oracle="oracle",
            allowed_source_path="src/a.py",
            diff_text="+line",
        )
        outcome = client.review_blinded_diff(req, DEFAULT_M29_NEUTRAL_EVALUATOR)
        assert not outcome.passed
        assert outcome.evaluator_identity_status == "unavailable"
        assert "http_status_502" in str(outcome.reason)


class TestDisposableDbMemoryIsolationManager:
    """Tests for DisposableDbMemoryIsolationManager memory loading and clearing."""

    def test_memory_loading_and_purging(self, tmp_path: Path) -> None:
        db_file = tmp_path / "test_iso.db"
        db_url = f"sqlite:///{db_file}"
        _setup_test_db(db_url)

        mgr = DisposableDbMemoryIsolationManager(db_url)

        # Baseline: empty personal, 1 project memory, 1 observation
        engine = create_engine(db_url)
        with Session(engine) as session:
            session.add(
                ProjectMemory(
                    repo_url="taskboard",
                    memory_key="arch",
                    value={"pattern": "mvc"},
                )
            )
            session.add(
                MemoryObservation(
                    repo_url="taskboard",
                    source="worker",
                    event_type="build",
                    summary="build pass",
                    content="build output",
                )
            )
            session.commit()

        mem = mgr.load_memory_context("taskboard")
        assert len(mem.personal) == 0
        assert len(mem.project) == 1
        assert mem.project[0].memory_key == "arch"
        assert len(mem.observations) == 1

        # Simulate case write (e.g. personal memory leak or observation creation)
        with Session(engine) as session:
            session.add(
                PersonalMemory(
                    memory_key="user_name",
                    value={"name": "Alice"},
                )
            )
            session.commit()

        # Loading should now fail due to personal memory isolation check
        with pytest.raises(ValueError, match="personal memory is not empty"):
            mgr.load_memory_context("taskboard")

        # Discard writes
        mgr.discard_case_memory_writes("taskboard")

        # Now loading succeeds again
        mem_after = mgr.load_memory_context("taskboard")
        assert len(mem_after.personal) == 0


class TestHttpStackCandidateDispatcher:
    """Tests for HttpStackCandidateDispatcher interaction and terminal capture."""

    def test_dispatch_and_polling_terminal_success(self, tmp_path: Path) -> None:
        db_file = tmp_path / "test_disp.db"
        db_url = f"sqlite:///{db_file}"
        _setup_test_db(db_url)

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir(parents=True)
        task_id = "task-disp-123"
        ws_id = "ws-disp-123"
        ws_path = ws_root / ws_id
        ws_path.mkdir(parents=True)
        (ws_path / "file.txt").write_text("hello world\n")

        task_snapshot_payload = {
            "task_id": task_id,
            "session_id": "sess-1",
            "status": "completed",
            "task_text": "sample task",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "latest_run": {
                "run_id": "run-1",
                "worker_type": "codex",
                "workspace_id": ws_id,
                "status": "completed",
                "started_at": datetime.now(UTC).isoformat(),
                "budget_usage": {
                    "native_agent": {
                        "model_execution": {
                            "provider": "codex",
                            "model": "gpt-6-luna",
                            "reasoning_effort": "high",
                        }
                    }
                },
            },
            "pending_interactions": [],
            "timeline": [],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                return httpx.Response(status_code=202, json=task_snapshot_payload)
            if request.method == "GET" and request.url.path == f"/tasks/{task_id}":
                return httpx.Response(status_code=200, json=task_snapshot_payload)
            return httpx.Response(status_code=404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        bundle_dir = tmp_path / "bundle"
        watchdog = EvaluationRetentionWatchdog(bundle_dir)

        dispatcher = HttpStackCandidateDispatcher(
            api_base_url="http://testserver",
            db_url=db_url,
            workspace_root=ws_root,
            repo_key="taskboard",
            retention_watchdog=watchdog,
            httpx_client=client,
        )

        case = _make_sample_case()
        result = dispatcher.dispatch_case(case, MemoryContext())

        assert result.task_id == task_id
        assert result.task_status == "completed"
        assert not result.pending_interaction
        assert result.execution_identity == ExecutionIdentity(
            provider="codex", model="gpt-6-luna", reasoning_effort="high"
        )
        assert result.execution_identity_status == "verified"
        assert len(watchdog.records) == 1
        assert watchdog.records[0].workspace_id == ws_id

        # Verify terminal snapshot immutability capture
        snap = dispatcher.capture_terminal_snapshot(case, result)
        assert snap.task_status == "completed"
        assert snap.checksum is not None

    def test_pending_interaction_early_stop(self, tmp_path: Path) -> None:
        db_file = tmp_path / "test_disp2.db"
        db_url = f"sqlite:///{db_file}"
        _setup_test_db(db_url)

        ws_root = tmp_path / "workspaces"
        ws_root.mkdir(parents=True)
        task_id = "task-hitl-1"

        snapshot_with_interaction = {
            "task_id": task_id,
            "session_id": "sess-2",
            "status": "paused",
            "task_text": "sample task",
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "pending_interactions": [
                {
                    "interaction_id": "int-1",
                    "interaction_type": "approval",
                    "status": "pending",
                    "summary": "approve delete?",
                    "created_at": datetime.now(UTC).isoformat(),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ],
            "timeline": [],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code=200, json=snapshot_with_interaction)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        dispatcher = HttpStackCandidateDispatcher(
            api_base_url="http://testserver",
            db_url=db_url,
            workspace_root=ws_root,
            httpx_client=client,
        )

        case = _make_sample_case()
        result = dispatcher.dispatch_case(case, MemoryContext())
        assert result.pending_interaction
        assert result.terminal_event_type == "interaction_requested"


class TestOperatorCliSubcommands:
    """Tests for scripts/e2e/run_m29_mutation_wave.py CLI subcommands."""

    def test_init_and_status_subcommands(self, tmp_path: Path) -> None:
        bundle_dir = tmp_path / "bundle"
        fixture_dir = tmp_path / "fixture"

        # init without ack flag must fail
        rc_no_ack = cli_main(
            ["init", "--bundle-dir", str(bundle_dir), "--fixture-repo-path", str(fixture_dir)]
        )
        assert rc_no_ack == 2

        # init with ack flag succeeds
        rc_init = cli_main(
            [
                "init",
                "--bundle-dir",
                str(bundle_dir),
                "--fixture-repo-path",
                str(fixture_dir),
                "--ack-disposable-mutation-stack",
            ]
        )
        assert rc_init == 0
        assert (bundle_dir / "bundle.json").exists()

        # status subcommand succeeds
        rc_status = cli_main(["status", "--bundle-dir", str(bundle_dir)])
        assert rc_status == 0

        # sweep-retention subcommand succeeds
        ws_root = tmp_path / "workspaces"
        ws_root.mkdir(parents=True)
        rc_sweep = cli_main(
            [
                "sweep-retention",
                "--bundle-dir",
                str(bundle_dir),
                "--workspace-root",
                str(ws_root),
            ]
        )
        assert rc_sweep == 0
