"""Persistence-path coverage for preflight-only reliability exclusions."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from db.base import Base
from db.enums import OrchestrationRuntime, TaskStatus, WorkerRuntimeMode, WorkerType
from evaluation.provider_reliability_extractor import extract_provider_reliability_report
from evaluation.provider_reliability_models import ReliabilityReportPolicy
from orchestrator.execution_outcome_service import _persist_execution_outcome
from orchestrator.graph import execute_with_preflight
from orchestrator.provider_diagnostics import ProviderDiagnosticsService
from orchestrator.provider_diagnostics_types import ProviderPreDispatchReport
from orchestrator.state import (
    OrchestratorState,
    RouteDecision,
    TaskRequest,
    TaskSpec,
    TaskTimelineEventState,
    WorkerDispatch,
)
from repositories import (
    SessionRepository,
    TaskRepository,
    UserRepository,
    create_engine_from_url,
    create_session_factory,
    session_scope,
)
from tests.integration.provider_reliability_support import NOW
from workers.base import WorkerRequest, WorkerResult


class _PersistenceWorker:
    """Minimal worker double proving the preflight gate prevents execution."""

    worker_type = "codex"
    default_runtime_mode = "native_agent"

    def __init__(self) -> None:
        self.run_called = False

    async def run(self, _request: object) -> object:
        self.run_called = True
        raise AssertionError("preflight-only test must not launch the worker")


class _OutcomePersistenceService:
    """Small production-persistence facade used by the integration test."""

    def __init__(self, session_factory: object) -> None:
        self.session_factory = session_factory
        self.retention_seconds = None

    def _prune_retained_runs(self, now: object) -> None:
        del now


def _rejection_report() -> ProviderPreDispatchReport:
    return ProviderPreDispatchReport(
        report_id="persisted-preflight-rejection",
        checked_at=NOW,
        execution_environment_id="test",
        logical_execution_key="persisted-preflight:attempt:1:primary",
        provider="codex",
        worker_profile="codex-native-executor",
        runtime_mode="native_agent",
        decision="preflight_rejected",
        ready=False,
        execution_started=False,
        failure_kind="provider_auth",
        next_action_hint="configure_codex_auth",
        summary="PRE_DISPATCH_DIAGNOSTIC_FAILURE: credential delivery is unavailable.",
    )


def _create_task(session_factory: object) -> str:
    with session_scope(session_factory) as session:
        user = UserRepository(session).create(external_user_id="preflight-test")
        conversation = SessionRepository(session).create(
            user_id=user.id,
            channel="http",
            external_thread_id="preflight-thread",
        )
        task = TaskRepository(session).create(
            session_id=conversation.id,
            task_text="Persist a preflight rejection",
            status=TaskStatus.PENDING,
            chosen_worker=WorkerType.CODEX,
            chosen_profile="codex-native-executor",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            orchestration_runtime=OrchestrationRuntime.TEMPORAL,
            task_spec={
                "goal": "Persist a preflight rejection",
                "task_type": "feature",
                "allowed_actions": ["modify_workspace_files"],
                "delivery_mode": "workspace",
            },
        )
        task.created_at = NOW
        task.updated_at = NOW
        return task.id


async def _execute_rejection(task_id: str) -> WorkerResult:
    worker = _PersistenceWorker()
    diagnostics = ProviderDiagnosticsService()
    request = WorkerRequest(
        task_id=task_id,
        session_id="preflight-thread",
        task_text="Persist a preflight rejection",
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        worker_profile="codex-native-executor",
    )
    with patch.object(
        diagnostics,
        "evaluate_pre_dispatch",
        new_callable=AsyncMock,
        return_value=_rejection_report(),
    ):
        result, _ = await execute_with_preflight(
            worker,
            request,
            worker_type="codex",
            session_id="preflight-thread",
            task_id=task_id,
            timeout_seconds=30,
            diagnostics_service=diagnostics,
        )
    assert not worker.run_called
    assert result.preflight_rejected
    assert result.execution_not_started
    return result


def _persist_rejection(session_factory: object, task_id: str, result: WorkerResult) -> None:
    state = OrchestratorState(
        task=TaskRequest(task_id=task_id, task_text="Persist a preflight rejection"),
        route=RouteDecision(
            chosen_worker="codex",
            chosen_profile="codex-native-executor",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            route_reason="test",
        ),
        dispatch=WorkerDispatch(
            worker_type="codex",
            worker_profile="codex-native-executor",
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        ),
        task_spec=TaskSpec(
            goal="Persist a preflight rejection",
            task_type="feature",
            allowed_actions=["modify_workspace_files"],
            delivery_mode="workspace",
        ),
        result=result,
        attempt_count=1,
        timeline_events=[
            TaskTimelineEventState(
                event_type="task_ingested",
                attempt_number=1,
                sequence_number=0,
                created_at=NOW,
            ),
            TaskTimelineEventState(
                event_type="task_failed",
                attempt_number=1,
                sequence_number=1,
                created_at=NOW,
            ),
        ],
    )
    _persist_execution_outcome(
        _OutcomePersistenceService(session_factory),
        task_id=task_id,
        state=state,
        started_at=NOW,
        finished_at=NOW,
        persist_friction_proposals=False,
    )


@pytest.mark.asyncio
async def test_preflight_rejection_persists_into_extractor_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the gate and production outcome persistence before extraction."""
    monkeypatch.setenv("CODE_AGENT_PRE_DISPATCH_DIAGNOSTICS_ENABLED", "1")
    db_url = f"sqlite+pysqlite:///{tmp_path / 'persisted_preflight_rejection.db'}"
    engine = create_engine_from_url(db_url)
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    task_id = _create_task(session_factory)
    result = await _execute_rejection(task_id)
    _persist_rejection(session_factory, task_id, result)

    policy = ReliabilityReportPolicy(
        as_of=NOW,
        window_start_at=NOW - timedelta(days=30),
        window_end_at=NOW + timedelta(days=1),
        min_samples=1,
    )
    report = extract_provider_reliability_report(db_url, policy)

    assert report.exclusions.by_reason["preflight_only_rejection"] == 1
    assert report.exclusions.included_tasks_count == 0
