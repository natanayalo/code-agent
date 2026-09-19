"""Integration tests for provider-specific pre-dispatch diagnostics execution boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from db.base import utc_now
from db.enums import ArtifactType
from orchestrator.graph import build_await_result_node, execute_with_preflight
from orchestrator.provider_diagnostics import ProviderDiagnosticsService, clear_probe_cache
from orchestrator.provider_diagnostics_types import (
    DiagnosticCheckResult,
    ProviderPreDispatchReport,
)
from orchestrator.state import OrchestratorState, RouteDecision, TaskRequest
from scripts.check_provider_diagnostics import _async_main, _parse_args
from workers.base import Worker, WorkerRequest, WorkerResult


class FakeWorker(Worker):
    worker_type: str = "codex"
    default_runtime_mode: str = "native_agent"

    def __init__(self) -> None:
        self.run_called = False

    async def run(self, request: WorkerRequest) -> WorkerResult:
        self.run_called = True
        return WorkerResult(
            status="success",
            summary="Worker completed work successfully.",
            commands_run=[{"command": "pytest"}],
            files_changed=["solution.py"],
        )


@pytest.fixture(autouse=True)
def reset_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED", "1")
    clear_probe_cache()
    yield
    clear_probe_cache()


def _make_state(
    *,
    task_id: str = "task-integration-1",
    worker_type: str = "codex",
    worker_profile: str = "codex-native",
    attempt_count: int = 1,
) -> OrchestratorState:
    return OrchestratorState(
        task=TaskRequest(task_id=task_id, task_text="Integration test task"),
        route=RouteDecision(
            chosen_worker=worker_type,
            chosen_profile=worker_profile,
            route_reason="test_routing",
        ),
        attempt_count=attempt_count,
    )


@pytest.mark.asyncio
async def test_single_worker_preflight_rejection_blocks_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_auth_dir = tmp_path / "empty_auth"
    empty_auth_dir.mkdir()
    monkeypatch.setenv("CODE_AGENT_CODEX_AUTH_DIR", str(empty_auth_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    worker = FakeWorker()
    await_node = build_await_result_node(worker=worker)
    state = _make_state(worker_type="codex")

    # Docker is responsive, but codex credentials are completely missing
    with patch.object(
        ProviderDiagnosticsService, "check_docker_daemon", new_callable=AsyncMock
    ) as mock_docker:
        mock_docker.return_value = DiagnosticCheckResult(
            name="docker_daemon",
            category="container_runtime",
            status="ready",
            detail="Docker up",
            verification_scope="local_runtime",
            blocking=False,
        )
        output = await await_node(state)

    assert not worker.run_called, "Worker run must NOT be invoked when preflight fails."
    res = output["result"]
    assert res["status"] == "error"
    assert res["failure_kind"] == "provider_auth"
    assert res["next_action_hint"] == "configure_codex_auth"
    assert "PRE_DISPATCH_DIAGNOSTIC_FAILURE" in res["summary"]

    diag_artifacts = [
        a for a in res["artifacts"] if a["artifact_type"] == ArtifactType.PRE_DISPATCH_DIAGNOSTICS
    ]
    assert len(diag_artifacts) == 1
    diag_art = diag_artifacts[0]
    assert diag_art["name"] == "pre-dispatch-diagnostics.json"
    assert diag_art["uri"].startswith("diagnostics://")
    assert diag_art["artifact_metadata"]["decision"] == "preflight_rejected"
    assert not diag_art["artifact_metadata"]["ready"]


@pytest.mark.asyncio
async def test_single_worker_preflight_success_attaches_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorker()
    await_node = build_await_result_node(worker=worker)
    state = _make_state(worker_type="codex")

    report = ProviderPreDispatchReport(
        report_id="test-report-passed-1234",
        checked_at=utc_now(),
        execution_environment_id="local",
        logical_execution_key="task-integration-1:attempt:1:primary",
        provider="codex",
        worker_profile="codex-native",
        runtime_mode="native_agent",
        model=None,
        decision="preflight_passed",
        ready=True,
        execution_started=False,
        checks=[],
    )

    with patch.object(
        ProviderDiagnosticsService, "evaluate_pre_dispatch", new_callable=AsyncMock
    ) as mock_eval:
        mock_eval.return_value = report
        output = await await_node(state)

    assert worker.run_called, "Worker should be invoked when preflight succeeds."
    res = output["result"]
    assert res["status"] == "success"

    diag_artifacts = [
        a for a in res["artifacts"] if a["artifact_type"] == ArtifactType.PRE_DISPATCH_DIAGNOSTICS
    ]
    assert len(diag_artifacts) == 1
    assert diag_artifacts[0]["uri"] == "diagnostics://test-report-passed-1234"
    assert diag_artifacts[0]["artifact_metadata"]["ready"] is True


@pytest.mark.asyncio
async def test_preflight_bypassed_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED", "0")
    worker = FakeWorker()

    request = WorkerRequest(
        task_text="Run with preflight disabled",
        session_id="sess-disabled",
    )

    with patch.object(
        ProviderDiagnosticsService, "evaluate_pre_dispatch", new_callable=AsyncMock
    ) as mock_eval:
        result, progress = await execute_with_preflight(
            worker,
            request,
            worker_type="codex",
            session_id="sess-disabled",
            timeout_seconds=30,
        )
        mock_eval.assert_not_called()

    assert worker.run_called
    assert result.status == "success"
    assert len(result.artifacts) == 0


@pytest.mark.asyncio
async def test_hanging_preflight_times_out_without_launching_worker() -> None:
    worker = FakeWorker()
    service = ProviderDiagnosticsService(preflight_timeout=0.01)
    request = WorkerRequest(task_text="Hanging preflight", session_id="sess-timeout")

    async def hang(_context: object) -> ProviderPreDispatchReport:
        await asyncio.sleep(100)
        raise AssertionError("unreachable")

    with patch.object(service, "evaluate_pre_dispatch", side_effect=hang):
        result, progress = await execute_with_preflight(
            worker,
            request,
            worker_type="codex",
            session_id="sess-timeout",
            timeout_seconds=30,
            diagnostics_service=service,
        )

    assert not worker.run_called
    assert result.status == "error"
    assert result.failure_kind == "timeout"
    assert result.preflight_rejected
    assert "worker launch was skipped" in (result.summary or "")
    assert "timed out" in progress


@pytest.mark.asyncio
async def test_preflight_elapsed_time_is_deducted_from_worker_timeout() -> None:
    worker = FakeWorker()
    service = ProviderDiagnosticsService(preflight_timeout=1.0)
    request = WorkerRequest(task_text="Slow preflight", session_id="sess-budget")
    report = ProviderPreDispatchReport(
        report_id="slow-preflight-report",
        checked_at=utc_now(),
        execution_environment_id="local",
        logical_execution_key="sess-budget:attempt:1:primary",
        provider="codex",
        runtime_mode="native_agent",
        decision="preflight_passed",
        ready=True,
        execution_started=False,
    )

    async def slow_ready(_context: object) -> ProviderPreDispatchReport:
        await asyncio.sleep(0.02)
        return report

    with patch.object(service, "evaluate_pre_dispatch", side_effect=slow_ready):
        result, progress = await execute_with_preflight(
            worker,
            request,
            worker_type="codex",
            session_id="sess-budget",
            timeout_seconds=0.01,
            diagnostics_service=service,
        )

    assert not worker.run_called
    assert result.failure_kind == "timeout"
    assert "consumed the execution timeout envelope" in progress


@pytest.mark.asyncio
async def test_decomposed_node_distinct_report_ids_for_parallel_nodes() -> None:
    worker = FakeWorker()
    req = WorkerRequest(task_text="Decomposed node 1", session_id="sess-dag")

    service = ProviderDiagnosticsService()
    with patch.object(service, "evaluate_pre_dispatch", wraps=service.evaluate_pre_dispatch):
        with patch.object(service, "check_docker_daemon", new_callable=AsyncMock) as mock_docker:
            mock_docker.return_value = DiagnosticCheckResult(
                name="docker_daemon",
                category="container_runtime",
                status="unready",
                detail="daemon down",
                verification_scope="local_runtime",
                blocking=True,
            )

            res1, _ = await execute_with_preflight(
                worker,
                req,
                worker_type="codex",
                session_id="sess-dag",
                timeout_seconds=30,
                diagnostics_service=service,
                task_id="t-parallel",
                attempt_count=1,
                logical_execution_key="plan-1:node-A:attempt:1",
            )
            res2, _ = await execute_with_preflight(
                worker,
                req,
                worker_type="codex",
                session_id="sess-dag",
                timeout_seconds=30,
                diagnostics_service=service,
                task_id="t-parallel",
                attempt_count=1,
                logical_execution_key="plan-1:node-B:attempt:1",
            )

    assert not worker.run_called
    assert res1.status == "error"
    assert res2.status == "error"

    art1 = res1.artifacts[0]
    art2 = res2.artifacts[0]
    assert art1.uri != art2.uri, "Parallel nodes must have distinct diagnostic report IDs."


@pytest.mark.asyncio
async def test_cli_diagnostics_runner_exit_codes() -> None:
    with patch.object(
        ProviderDiagnosticsService, "evaluate_all_providers", new_callable=AsyncMock
    ) as mock_eval:
        from db.base import utc_now
        from orchestrator.provider_diagnostics_types import SystemProviderDiagnosticsReport

        now = utc_now()
        report_ok = SystemProviderDiagnosticsReport(
            checked_at=now,
            expires_at=now,
            target="cli",
            execution_environment_id="local",
            providers={},
            all_ready=False,
            required_providers_ready=True,
        )
        report_fail = SystemProviderDiagnosticsReport(
            checked_at=now,
            expires_at=now,
            target="cli",
            execution_environment_id="local",
            providers={},
            all_ready=False,
            required_providers_ready=False,
        )

        mock_eval.return_value = report_ok
        args_ok = _parse_args(["--required", "codex", "--json"])
        code_ok = await _async_main(args_ok)
        assert code_ok == 0

        mock_eval.return_value = report_fail
        args_fail = _parse_args(["--required", "codex", "--json"])
        code_fail = await _async_main(args_fail)
        assert code_fail == 1


@pytest.mark.asyncio
async def test_cli_diagnostics_filters_target_providers_and_rejects_unknown_required(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from orchestrator.provider_diagnostics_types import SystemProviderDiagnosticsReport

    with patch.object(
        ProviderDiagnosticsService, "evaluate_all_providers", new_callable=AsyncMock
    ) as mock_eval:
        now = utc_now()
        mock_eval.return_value = SystemProviderDiagnosticsReport(
            checked_at=now,
            expires_at=now,
            target="cli",
            execution_environment_id="local",
            providers={},
            all_ready=True,
            required_providers_ready=True,
        )
        code = await _async_main(_parse_args(["--providers", "codex"]))
        assert code == 0
        mock_eval.assert_awaited_once_with(
            required_providers=None,
            target="cli",
            target_providers={"codex"},
        )

        mock_eval.reset_mock()
        code = await _async_main(_parse_args(["--required", "nonexistent"]))
        assert code == 2
        mock_eval.assert_not_awaited()
        assert "Unknown required provider(s): nonexistent" in capsys.readouterr().err
